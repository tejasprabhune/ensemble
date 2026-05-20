use std::sync::Arc;

use once_cell::sync::Lazy;
use parking_lot::Mutex;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyList;

use ensemble_core::actor::ActorHandle;
use ensemble_core::bus::{Bus, Message, Recipient};
use ensemble_core::event::{EventLog, EventPayload};
use ensemble_core::ids::{ActorId, MessageId};
use ensemble_core::predicate::{PredicateCtx, PredicateRegistry};
use ensemble_core::scheduler::{Scheduler, StopReason, TickBudget};
use ensemble_core::until::{turn_count_exceeds, Until, UntilCtx};
use ensemble_runtime::{
    AgentActor, AnthropicBackend, HiddenState, LocalAdapterBackend, MockBackend, MockScript,
    MockTurn, OpenAIBackend, PromptedPersona, SharedBackend, ToolRegistry, UserActor,
};

mod world_registry;
use world_registry::{WorldBundle, WorldRegistry};

fn noop_world_builder() -> WorldBundle {
    WorldBundle {
        tools: ToolRegistry::new(),
        predicates: PredicateRegistry::new(),
    }
}

fn plank_world_builder() -> WorldBundle {
    let (_state, tools, predicates) = plank::build();
    // The state Arc lives on inside each tool and predicate closure;
    // the bundle holds them alive for as long as the world instance.
    WorldBundle { tools, predicates }
}

/// Inner world state shared between `World`, `User`, and `Agent`.
/// Actor specs and seed messages accumulate here and are consumed at
/// `run()` time (wired up in a later commit).
pub(crate) struct WorldInner {
    pub(crate) name: String,
    pub(crate) bus: Bus,
    pub(crate) log: EventLog,
    pub(crate) backend: SharedBackend,
    pub(crate) backend_kind: BackendKind,
    pub(crate) script: MockScript,
    pub(crate) tools: Arc<ToolRegistry>,
    pub(crate) predicates: Arc<PredicateRegistry>,
    pub(crate) actors: Vec<ActorSpec>,
    pub(crate) seed_messages: Vec<(ActorId, ActorId, Message)>,
    pub(crate) budget: TickBudget,
    pub(crate) bg_task:
        Option<tokio::task::JoinHandle<Result<StopReason, ensemble_core::error::CoreError>>>,
    pub(crate) registered_inboxes: Vec<ActorId>,
}

#[derive(Clone, Copy, Eq, PartialEq, Debug)]
pub(crate) enum BackendKind {
    Mock,
    Anthropic,
    OpenAI,
    Vllm,
}

#[derive(Clone)]
pub(crate) struct ActorSpec {
    pub(crate) id: ActorId,
    pub(crate) kind: SpecKind,
    #[allow(dead_code)]
    pub(crate) persona: Option<String>,
    #[allow(dead_code)]
    pub(crate) hidden_goal: Option<String>,
    pub(crate) model: Option<String>,
    // TODO: filter the world's ToolRegistry by this list before
    // handing it to the AgentActor. Today every agent sees every
    // registered tool, which is fine for the worked example but not
    // for adversarial scenarios with restricted-capability agents.
    #[allow(dead_code)]
    pub(crate) tools: Vec<String>,
    pub(crate) system_prompt: Option<String>,
    /// Shared HiddenState handle. Populated when the python layer
    /// resolved a persona TOML and computed initial hidden state. The
    /// same Arc is given to the spawned `User` pyclass so post-run
    /// reads see whatever the persona mutated to.
    pub(crate) hidden: Option<HiddenState>,
}

#[derive(Clone, Copy, Eq, PartialEq, Debug)]
pub(crate) enum SpecKind {
    User,
    Agent,
}

#[pyclass]
pub struct World {
    pub(crate) inner: Arc<Mutex<WorldInner>>,
}

#[pymethods]
impl World {
    /// `backend` selects the LLM client used by every actor in this
    /// world. Choices: `"mock"` (default; deterministic, no network),
    /// `"anthropic"` (Messages API, needs ANTHROPIC_API_KEY),
    /// `"openai"` (Chat Completions, needs OPENAI_API_KEY),
    /// `"vllm"` (OpenAI-shaped HTTP endpoint, set `base_url`), or
    /// `"auto"` (pick the first backend whose API key is in the env,
    /// fall back to mock).
    #[new]
    #[pyo3(signature = (name=None, backend=None, base_url=None))]
    fn new(
        name: Option<&str>,
        backend: Option<&str>,
        base_url: Option<&str>,
    ) -> PyResult<Self> {
        let name = name.unwrap_or("noop").to_string();
        let bundle = WorldRegistry::build(&name).ok_or_else(|| {
            PyValueError::new_err(format!(
                "no world named {name:?}; register one before constructing it"
            ))
        })?;
        let script = MockScript::new();
        let (backend, kind) = build_backend(backend, base_url, &script)?;
        let log = EventLog::new();
        let bus = Bus::new(log.clone());
        // Real LLM round trips can easily blow past the 500ms default
        // quiescence window; bump it whenever the backend is anything
        // other than mock so the watcher does not abort an in-flight
        // HTTP call.
        let mut budget = TickBudget::default();
        if !matches!(kind, BackendKind::Mock) {
            budget.quiescence_ms = 60_000;
        }
        Ok(Self {
            inner: Arc::new(Mutex::new(WorldInner {
                name,
                bus,
                log,
                backend,
                backend_kind: kind,
                script,
                tools: Arc::new(bundle.tools),
                predicates: Arc::new(bundle.predicates),
                actors: vec![],
                seed_messages: vec![],
                budget,
                bg_task: None,
                registered_inboxes: vec![],
            })),
        })
    }

    /// Name of the active backend (`"mock"`, `"anthropic"`,
    /// `"openai"`, `"vllm"`).
    #[getter]
    fn backend(&self) -> &'static str {
        match self.inner.lock().backend_kind {
            BackendKind::Mock => "mock",
            BackendKind::Anthropic => "anthropic",
            BackendKind::OpenAI => "openai",
            BackendKind::Vllm => "vllm",
        }
    }

    #[getter]
    fn name(&self) -> String {
        self.inner.lock().name.clone()
    }

    /// Count of actors registered on this world.
    fn actor_count(&self) -> usize {
        self.inner.lock().actors.len()
    }

    #[pyo3(signature = (id=None, persona=None, hidden_goal=None, model="user-model", system_prompt=None, hidden_state_json=None))]
    fn spawn_user(
        &self,
        id: Option<&str>,
        persona: Option<&str>,
        hidden_goal: Option<&str>,
        model: &str,
        system_prompt: Option<&str>,
        hidden_state_json: Option<&str>,
    ) -> PyResult<User> {
        let actor_id = ActorId::from_label(id.unwrap_or_else(|| persona.unwrap_or("user")));
        let hidden = match hidden_state_json {
            Some(s) => {
                let v: serde_json::Value = serde_json::from_str(s).map_err(|e| {
                    PyValueError::new_err(format!("hidden_state_json: bad json: {e}"))
                })?;
                Some(HiddenState::new(v))
            }
            None => None,
        };
        let spec = ActorSpec {
            id: actor_id.clone(),
            kind: SpecKind::User,
            persona: persona.map(str::to_string),
            hidden_goal: hidden_goal.map(str::to_string),
            model: Some(model.into()),
            tools: vec![],
            system_prompt: system_prompt.map(str::to_string),
            hidden: hidden.clone(),
        };
        self.inner.lock().actors.push(spec);
        Ok(User {
            id: actor_id.to_string(),
            world: self.inner.clone(),
            hidden,
        })
    }

    #[pyo3(signature = (id=None, model="claude-sonnet-4-5", tools=None, system_prompt=None))]
    fn spawn_agent(
        &self,
        id: Option<&str>,
        model: &str,
        tools: Option<&Bound<'_, PyList>>,
        system_prompt: Option<&str>,
    ) -> PyResult<Agent> {
        let actor_id = ActorId::from_label(id.unwrap_or("agent"));
        let tool_names: Vec<String> = match tools {
            Some(list) => list
                .iter()
                .map(|item| item.extract::<String>())
                .collect::<PyResult<_>>()?,
            None => vec![],
        };
        let spec = ActorSpec {
            id: actor_id.clone(),
            kind: SpecKind::Agent,
            persona: None,
            hidden_goal: None,
            model: Some(model.into()),
            tools: tool_names,
            system_prompt: system_prompt.map(str::to_string),
            hidden: None,
        };
        self.inner.lock().actors.push(spec);
        Ok(Agent {
            id: actor_id.to_string(),
            world: self.inner.clone(),
        })
    }

    /// Test-only: queue a canned mock response for a given model.
    fn _mock_say(&self, model: &str, text: &str) {
        self.inner.lock().script.push_for(model, MockTurn::text(text));
    }

    /// Test-only: queue a canned mock turn that emits both text and a
    /// tool call in one step. Matches how real frontier models often
    /// preface a tool use.
    fn _mock_say_then_tool(
        &self,
        model: &str,
        text: &str,
        tool: &str,
        args_json: &str,
    ) -> PyResult<()> {
        let args: serde_json::Value = serde_json::from_str(args_json)
            .map_err(|e| PyValueError::new_err(format!("bad json: {e}")))?;
        self.inner
            .lock()
            .script
            .push_for(model, MockTurn::say_then_tool(text, tool, args));
        Ok(())
    }

    /// Test-only: queue a canned mock tool call for a given model.
    fn _mock_tool(&self, model: &str, tool: &str, args_json: &str) -> PyResult<()> {
        let args: serde_json::Value = serde_json::from_str(args_json)
            .map_err(|e| PyValueError::new_err(format!("bad json: {e}")))?;
        self.inner.lock().script.push_for(model, MockTurn::tool(tool, args));
        Ok(())
    }

    /// Build and run the scheduler synchronously. `until_spec` is a
    /// JSON spec like `{"kind":"turn_count_gt","n":30}`. Blocks the
    /// calling thread until the scheduler stops. A world only knows
    /// how to run once: spawn_user/spawn_agent populates a queue that
    /// is drained at run time, so calling run_until twice without
    /// re-spawning actors raises a clear error rather than silently
    /// running an empty world.
    fn run_until(&self, until_spec_json: &str) -> PyResult<()> {
        let spec: serde_json::Value = serde_json::from_str(until_spec_json)
            .map_err(|e| PyValueError::new_err(format!("bad until spec: {e}")))?;
        let until = build_until(&spec)?;
        let (bus, actor_handles, seed_messages, budget) = {
            let mut inner = self.inner.lock();
            if inner.actors.is_empty() && !inner.registered_inboxes.is_empty() {
                return Err(PyRuntimeError::new_err(
                    "world.run() called twice without re-spawning actors. \
                     Worlds run once per construction; build a fresh World \
                     for another scenario.",
                ));
            }
            let backend = inner.backend.clone();
            let tools = inner.tools.clone();
            let bus = inner.bus.clone();
            let budget = inner.budget;
            let mut handles: Vec<(ActorId, Arc<dyn ensemble_core::actor::Actor>)> = Vec::new();
            for spec in inner.actors.drain(..) {
                let actor = build_actor(spec, backend.clone(), tools.clone());
                handles.push(actor);
            }
            inner.registered_inboxes = handles.iter().map(|(id, _)| id.clone()).collect();
            let seed = inner.seed_messages.drain(..).collect::<Vec<_>>();
            (bus, handles, seed, budget)
        };

        let runtime = global_runtime();
        runtime.block_on(async move {
            let mut scheduler = Scheduler::new(bus.clone(), budget);
            for (id, actor) in actor_handles {
                let inbox = bus.register(id).await;
                scheduler.register(Arc::new(ActorHandle::new(actor, inbox)));
            }
            scheduler.set_until(until).await;
            for (from, to, msg) in seed_messages {
                bus.send(from, Recipient::Actor(to), msg).await.ok();
            }
            scheduler.run().await.map(|_stop| ())
        })
        .map_err(|e| PyRuntimeError::new_err(format!("scheduler error: {e}")))
    }

    /// Return the current log length. Mostly for testing; the scenario
    /// API exposes this as `world.turn_count` via a sentinel.
    fn current_turn_count(&self) -> PyResult<u64> {
        let log = self.inner.lock().log.clone();
        let runtime = global_runtime();
        Ok(runtime.block_on(log.len()) as u64)
    }

    /// Append a `system` event to the log. Used by the python wrapper
    /// to record the chosen backend so it shows up in saved traces and
    /// in the viewer.
    fn log_note(&self, note: &str) -> PyResult<()> {
        let bus = self.inner.lock().bus.clone();
        global_runtime().block_on(bus.append_event(
            None,
            ensemble_core::event::EventPayload::System { note: note.into() },
        ));
        Ok(())
    }

    /// Serialize the trace log to JSONL.
    fn trace_jsonl(&self) -> PyResult<String> {
        let log = self.inner.lock().log.clone();
        let runtime = global_runtime();
        runtime
            .block_on(log.to_jsonl())
            .map_err(|e| PyRuntimeError::new_err(format!("trace serialize: {e}")))
    }

    /// Start the scheduler in the background on the global tokio
    /// runtime. Returns immediately. The caller is expected to drive
    /// it via `wait_for_until` and stop it via `stop_scheduler`.
    fn start_scheduler(&self) -> PyResult<()> {
        let (bus, actor_handles, seed_messages, mut budget) = {
            let mut inner = self.inner.lock();
            if inner.bg_task.is_some() {
                return Err(PyRuntimeError::new_err("scheduler already started"));
            }
            let backend = inner.backend.clone();
            let tools = inner.tools.clone();
            let bus = inner.bus.clone();
            let mut handles: Vec<(ActorId, Arc<dyn ensemble_core::actor::Actor>)> = Vec::new();
            for spec in inner.actors.drain(..) {
                let actor = build_actor(spec, backend.clone(), tools.clone());
                handles.push(actor);
            }
            let seed = inner.seed_messages.drain(..).collect::<Vec<_>>();
            (bus, handles, seed, inner.budget)
        };
        // Loosen the budget: the python harness is in charge of when
        // to stop. Still keep quiescence so a stalled run halts.
        budget.max_ticks = budget.max_ticks.max(10_000);
        budget.max_events = budget.max_events.max(50_000);
        budget.quiescence_ms = budget.quiescence_ms.max(2_000);

        let runtime = global_runtime();
        let mut registered = Vec::new();
        let _enter = runtime.enter();
        let mut scheduler = Scheduler::new(bus.clone(), budget);
        for (id, actor) in actor_handles {
            let inbox = runtime.block_on(bus.register(id.clone()));
            registered.push(id.clone());
            scheduler.register(Arc::new(ActorHandle::new(actor, inbox)));
        }
        // Seed messages before starting actors so they have something
        // to react to.
        runtime.block_on(async {
            for (from, to, msg) in seed_messages {
                bus.send(from, Recipient::Actor(to), msg).await.ok();
            }
        });
        let task = runtime.spawn(async move { scheduler.run().await });
        let mut inner = self.inner.lock();
        inner.bg_task = Some(task);
        inner.registered_inboxes = registered;
        Ok(())
    }

    /// Block until the given until-spec fires (or the scheduler exits
    /// for any reason). Returns true if the predicate fired, false if
    /// the scheduler ended first.
    fn wait_for_until(&self, until_spec_json: &str, timeout_ms: u64) -> PyResult<bool> {
        let spec: serde_json::Value = serde_json::from_str(until_spec_json)
            .map_err(|e| PyValueError::new_err(format!("bad until spec: {e}")))?;
        let until = build_until(&spec)?;
        let bus = self.inner.lock().bus.clone();
        let log = self.inner.lock().log.clone();
        let runtime = global_runtime();
        let notifier = bus.notifier();
        let deadline = std::time::Instant::now() + std::time::Duration::from_millis(timeout_ms);

        Ok(runtime.block_on(async move {
            loop {
                let cur = log.len().await as u64;
                let ctx = UntilCtx { tick: cur, log: &log, events_seen: cur as usize };
                if until.check(&ctx) {
                    return true;
                }
                let now = std::time::Instant::now();
                if now >= deadline {
                    return false;
                }
                let remaining = deadline - now;
                tokio::select! {
                    _ = notifier.notified() => continue,
                    _ = tokio::time::sleep(remaining) => return false,
                }
            }
        }))
    }

    /// Abort the background scheduler task and wait for it to finish.
    fn stop_scheduler(&self) -> PyResult<()> {
        let task = { self.inner.lock().bg_task.take() };
        if let Some(task) = task {
            task.abort();
            let _ = global_runtime().block_on(async move { task.await });
        }
        Ok(())
    }

    /// True when the background scheduler is running.
    fn is_running(&self) -> bool {
        let inner = self.inner.lock();
        match &inner.bg_task {
            Some(t) => !t.is_finished(),
            None => false,
        }
    }

    /// Send a message from a registered actor while the scheduler is
    /// running. Used by `User.say` for mid-run intervention.
    fn send_now(&self, from: &str, target: &str, kind: &str, text: &str) -> PyResult<()> {
        let bus = self.inner.lock().bus.clone();
        let msg = match kind {
            "user" => Message::UserMessage { text: text.into() },
            "agent" => Message::AgentMessage { text: text.into() },
            other => {
                return Err(PyValueError::new_err(format!(
                    "unknown send_now kind: {other}"
                )))
            }
        };
        global_runtime()
            .block_on(bus.send(
                ActorId::from_label(from),
                Recipient::Actor(ActorId::from_label(target)),
                msg,
            ))
            .map_err(|e| PyRuntimeError::new_err(format!("send: {e}")))
    }

    /// Names of the predicates this world has registered. Mostly used
    /// for introspection in tests and docs.
    fn predicate_names(&self) -> Vec<String> {
        self.inner.lock().predicates.names()
    }

    /// Evaluate a named predicate against the current trace. Returns
    /// `None` if the predicate is not registered. Predicates are
    /// world-supplied: a Plank world exposes things like
    /// `had_double_refund` and `agent_recommended_upgrade`.
    #[pyo3(signature = (name, args_json=None))]
    fn evaluate_predicate(
        &self,
        name: &str,
        args_json: Option<&str>,
    ) -> PyResult<Option<bool>> {
        let args = match args_json {
            Some(s) => serde_json::from_str(s)
                .map_err(|e| PyValueError::new_err(format!("bad args json: {e}")))?,
            None => serde_json::Value::Null,
        };
        let (log, preds) = {
            let inner = self.inner.lock();
            (inner.log.clone(), inner.predicates.clone())
        };
        let runtime = global_runtime();
        let events = runtime.block_on(log.snapshot());
        Ok(preds.evaluate(name, &PredicateCtx::with_args(&events, args)))
    }

    /// Return the trace events as a list of JSON-encoded strings, one
    /// per event. Cheap to consume from Python via json.loads().
    fn trace_events(&self) -> PyResult<Vec<String>> {
        let log = self.inner.lock().log.clone();
        let runtime = global_runtime();
        let events = runtime.block_on(log.snapshot());
        events
            .into_iter()
            .map(|e| {
                serde_json::to_string(&e)
                    .map_err(|e| PyRuntimeError::new_err(format!("encode: {e}")))
            })
            .collect()
    }
}

#[pyclass]
pub struct User {
    #[pyo3(get)]
    id: String,
    world: Arc<Mutex<WorldInner>>,
    hidden: Option<HiddenState>,
}

#[pymethods]
impl User {
    /// Current hidden-state JSON snapshot. Returns `null` if this user
    /// has no persona (i.e. no hidden state was attached at spawn).
    /// Used by post-run graders that inspect what the persona did
    /// internally during the run.
    fn hidden_state_json(&self) -> String {
        match &self.hidden {
            Some(h) => h.snapshot().to_string(),
            None => "null".into(),
        }
    }

    /// Queue a seed message from this user to the named actor. The
    /// message is delivered when the scenario calls `run()`.
    fn say(&self, target: &str, text: &str) {
        self.world.lock().seed_messages.push((
            ActorId::from_label(&self.id),
            ActorId::from_label(target),
            Message::UserMessage { text: text.into() },
        ));
    }

    fn __repr__(&self) -> String {
        format!("<User id={:?}>", self.id)
    }

    /// Run a tool from this user immediately and record the call and
    /// result in the trace. The user is asserting that the action
    /// happened (it is part of the scenario's setup, not something a
    /// model decided), so the dispatch goes straight through the
    /// world's ToolRegistry rather than waiting for an LLM round trip.
    /// State mutations land in the world before the scheduler starts.
    fn act_json(&self, tool: &str, args_json: &str) -> PyResult<()> {
        let args: serde_json::Value = serde_json::from_str(args_json)
            .map_err(|e| PyValueError::new_err(format!("bad json: {e}")))?;
        let call_id = MessageId::new().to_string();
        let (bus, tools) = {
            let inner = self.world.lock();
            (inner.bus.clone(), inner.tools.clone())
        };
        let actor = ActorId::from_label(&self.id);
        let runtime = global_runtime();
        runtime.block_on(async {
            bus.append_event(
                Some(actor.clone()),
                EventPayload::ToolCall {
                    id: call_id.clone(),
                    name: tool.into(),
                    args: args.clone(),
                },
            )
            .await;
            match tools.dispatch(tool, &args) {
                Ok(effect) => {
                    bus.append_event(
                        Some(actor),
                        EventPayload::ToolResult {
                            id: call_id,
                            name: tool.into(),
                            result: effect,
                            is_error: false,
                        },
                    )
                    .await;
                }
                Err(e) => {
                    let err_json = serde_json::json!({"ok": false, "error": e.to_string()});
                    bus.append_event(
                        Some(actor),
                        EventPayload::ToolResult {
                            id: call_id,
                            name: tool.into(),
                            result: err_json,
                            is_error: true,
                        },
                    )
                    .await;
                }
            }
        });
        Ok(())
    }
}

#[pyclass]
pub struct Agent {
    #[pyo3(get)]
    id: String,
    world: Arc<Mutex<WorldInner>>,
}

#[pymethods]
impl Agent {
    fn say(&self, target: &str, text: &str) {
        self.world.lock().seed_messages.push((
            ActorId::from_label(&self.id),
            ActorId::from_label(target),
            Message::AgentMessage { text: text.into() },
        ));
    }

    fn __repr__(&self) -> String {
        format!("<Agent id={:?}>", self.id)
    }
}

/// Materialize an `ActorSpec` into a concrete user or agent actor.
/// When a user has both a system_prompt template and an attached
/// HiddenState, the world-shared backend is wrapped in a
/// PromptedPersona so the model sees the persona's hidden state on
/// every turn.
fn build_actor(
    spec: ActorSpec,
    backend: SharedBackend,
    tools: Arc<ToolRegistry>,
) -> (ActorId, Arc<dyn ensemble_core::actor::Actor>) {
    let id = spec.id.clone();
    let model = spec.model.clone().unwrap_or_else(|| match spec.kind {
        SpecKind::User => "user-model".into(),
        SpecKind::Agent => "agent-model".into(),
    });
    let backend_for_actor: SharedBackend = match (&spec.system_prompt, &spec.hidden) {
        (Some(template), Some(hidden)) => Arc::new(PromptedPersona::new(
            backend,
            model.clone(),
            template.clone(),
            hidden.clone(),
        )),
        _ => backend,
    };
    let actor: Arc<dyn ensemble_core::actor::Actor> = match spec.kind {
        SpecKind::User => {
            let mut a = UserActor::new(spec.id, model, backend_for_actor);
            if let (Some(sp), None) = (&spec.system_prompt, &spec.hidden) {
                a = a.with_system_prompt(sp.clone());
            }
            Arc::new(a)
        }
        SpecKind::Agent => {
            let mut a = AgentActor::new(spec.id, model, backend_for_actor, tools);
            if let Some(sp) = spec.system_prompt {
                a = a.with_system_prompt(sp);
            }
            Arc::new(a)
        }
    };
    (id, actor)
}

fn build_backend(
    name: Option<&str>,
    base_url: Option<&str>,
    script: &MockScript,
) -> PyResult<(SharedBackend, BackendKind)> {
    let chosen = match name.unwrap_or("mock") {
        "mock" => "mock",
        "anthropic" => "anthropic",
        "openai" => "openai",
        "vllm" => "vllm",
        "auto" => {
            if std::env::var("ANTHROPIC_API_KEY").is_ok() {
                "anthropic"
            } else if std::env::var("OPENAI_API_KEY").is_ok() {
                "openai"
            } else {
                "mock"
            }
        }
        other => {
            return Err(PyValueError::new_err(format!(
                "unknown backend {other:?}; choose mock | anthropic | openai | vllm | auto"
            )))
        }
    };
    Ok(match chosen {
        "mock" => (
            Arc::new(MockBackend::new(script.clone())) as SharedBackend,
            BackendKind::Mock,
        ),
        "anthropic" => {
            let mut be = AnthropicBackend::from_env()
                .map_err(|e| PyValueError::new_err(format!("{e}")))?;
            let url = base_url
                .map(str::to_string)
                .or_else(|| std::env::var("ANTHROPIC_BASE_URL").ok());
            if let Some(url) = url {
                be = be.with_base_url(url);
            }
            (Arc::new(be) as SharedBackend, BackendKind::Anthropic)
        }
        "openai" => {
            let mut be = OpenAIBackend::from_env()
                .map_err(|e| PyValueError::new_err(format!("{e}")))?;
            let url = base_url
                .map(str::to_string)
                .or_else(|| std::env::var("OPENAI_BASE_URL").ok());
            if let Some(url) = url {
                be = be.with_base_url(url);
            }
            (Arc::new(be) as SharedBackend, BackendKind::OpenAI)
        }
        "vllm" => {
            let base = base_url.ok_or_else(|| {
                PyValueError::new_err("vllm backend requires base_url=...")
            })?;
            let be = LocalAdapterBackend::new(base);
            (Arc::new(be) as SharedBackend, BackendKind::Vllm)
        }
        _ => unreachable!(),
    })
}

fn global_runtime() -> &'static tokio::runtime::Runtime {
    static RT: Lazy<tokio::runtime::Runtime> = Lazy::new(|| {
        tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .worker_threads(2)
            .build()
            .expect("build tokio runtime")
    });
    &RT
}

/// Parse an `until` spec dict into a Rust `Until` closure. Recognised
/// kinds: `turn_count_gt`, `turn_count_ge`, `any_of`, `all_of`.
fn build_until(spec: &serde_json::Value) -> PyResult<Until> {
    let kind = spec
        .get("kind")
        .and_then(|v| v.as_str())
        .ok_or_else(|| PyValueError::new_err("until spec missing 'kind'"))?;
    match kind {
        "turn_count_gt" => {
            let n = spec
                .get("n")
                .and_then(|v| v.as_u64())
                .ok_or_else(|| PyValueError::new_err("turn_count_gt requires 'n'"))?;
            Ok(Until::new(format!("turn_count > {n}"), move |ctx: &UntilCtx<'_>| {
                ctx.tick > n
            }))
        }
        "turn_count_ge" => {
            let n = spec
                .get("n")
                .and_then(|v| v.as_u64())
                .ok_or_else(|| PyValueError::new_err("turn_count_ge requires 'n'"))?;
            Ok(turn_count_exceeds(n))
        }
        "any_of" => {
            let parts = spec
                .get("parts")
                .and_then(|v| v.as_array())
                .ok_or_else(|| PyValueError::new_err("any_of requires 'parts'"))?;
            let built: Vec<Until> = parts
                .iter()
                .map(build_until)
                .collect::<PyResult<_>>()?;
            Ok(ensemble_core::until::any_of(built))
        }
        "all_of" => {
            let parts = spec
                .get("parts")
                .and_then(|v| v.as_array())
                .ok_or_else(|| PyValueError::new_err("all_of requires 'parts'"))?;
            let built: Vec<Until> = parts
                .iter()
                .map(build_until)
                .collect::<PyResult<_>>()?;
            Ok(ensemble_core::until::all_of(built))
        }
        other => Err(PyValueError::new_err(format!(
            "unknown until kind: {other}"
        ))),
    }
}

#[pymodule]
fn _native(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Always-available no-op world for tests and the scaffold flow.
    WorldRegistry::register("noop", noop_world_builder);
    // Plank is built-in for the MVP. Future worlds register via their
    // own pyo3 modules or by importing into this one.
    WorldRegistry::register("plank", plank_world_builder);

    m.add_class::<World>()?;
    m.add_class::<User>()?;
    m.add_class::<Agent>()?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
