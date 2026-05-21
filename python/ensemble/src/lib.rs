use std::collections::{HashMap, VecDeque};
use std::sync::Arc;

use async_trait::async_trait;
use once_cell::sync::Lazy;
use parking_lot::Mutex;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyList;

use ensemble_core::actor::ActorHandle;
use ensemble_core::bus::{Bus, Message, Recipient};
use ensemble_core::event::{EventLog, EventPayload, TraceFile};
use ensemble_core::ids::{ActorId, MessageId};
use ensemble_core::predicate::{PredicateCtx, PredicateRegistry};
use ensemble_core::scheduler::{Scheduler, StopReason, TickBudget};
use ensemble_core::until::{turn_count_exceeds, Until, UntilCtx};
use ensemble_runtime::{
    resources::ResourceManager, AgentActor, AnthropicBackend, HiddenState, LocalAdapterBackend,
    MockBackend, MockScript, MockTurn, OpenAIBackend, PromptedPersona, SharedBackend, Tool,
    ToolOutcome, ToolRegistry, UserActor,
};
use ensemble_core::error::ToolError;

mod world_registry;
use world_registry::{WorldBundle, WorldRegistry};

/// Build a world bundle with an empty tool registry and the
/// inherited default predicates. Used both for the built-in `noop`
/// world and as the fallback when a python plugin registers a name
/// we have not seen native-side; the plugin then fills in per-world
/// tools and predicates via `register_tool` / `register_predicate`.
fn empty_world_bundle() -> WorldBundle {
    WorldBundle {
        tools: ToolRegistry::new(),
        predicates: PredicateRegistry::with_defaults(),
    }
}

/// Inner world state shared between `World`, `User`, and `Agent`.
/// Actor specs and seed messages accumulate here and are consumed at
/// `run()` time (wired up in a later commit).
pub(crate) struct WorldInner {
    pub(crate) name: String,
    pub(crate) bus: Bus,
    pub(crate) log: EventLog,
    pub(crate) backend: SharedBackend,
    pub(crate) backend_kind: &'static str,
    pub(crate) script: MockScript,
    pub(crate) tools: Arc<ToolRegistry>,
    pub(crate) predicates: Arc<PredicateRegistry>,
    pub(crate) resources: Arc<ResourceManager>,
    pub(crate) actors: Vec<ActorSpec>,
    pub(crate) seed_messages: Vec<(ActorId, ActorId, Message)>,
    pub(crate) budget: TickBudget,
    pub(crate) bg_task:
        Option<tokio::task::JoinHandle<Result<StopReason, ensemble_core::error::CoreError>>>,
    pub(crate) registered_inboxes: Vec<ActorId>,
    /// Per-agent message queues for externally driven slots, keyed by
    /// agent id. Populated when `register_external_agent` is called.
    pub(crate) external_inboxes: HashMap<ActorId, ExternalAgentInbox>,
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
    /// Names of tools this agent can call. `None` means unrestricted
    /// (the agent sees every tool the world registered);
    /// `Some(names)` filters both the schemas the model sees and the
    /// dispatcher's accept-list. `Some(empty)` is the bare-NPC case:
    /// the agent has no tools, and any hallucinated call lands as an
    /// is_error tool result in the trace.
    pub(crate) tools: Option<Vec<String>>,
    pub(crate) system_prompt: Option<String>,
    /// Per-actor LLM knobs. Forwarded into the agent's
    /// CompletionRequest as `extra_params`; the runtime passes them
    /// through to the backend without interpretation, so a scenario
    /// that wants `reasoning_effort=high` or `top_p=0.9` on a single
    /// agent does not need a typed field in the runtime for every
    /// backend's options.
    pub(crate) extra_params: std::collections::HashMap<String, serde_json::Value>,
    /// Shared HiddenState handle. Populated when the python layer
    /// resolved a persona TOML and computed initial hidden state. The
    /// same Arc is given to the spawned `User` pyclass so post-run
    /// reads see whatever the persona mutated to.
    pub(crate) hidden: Option<HiddenState>,
    /// Inbox shared with the python layer when this spec is an
    /// external agent slot. The scheduler-spawned actor pushes
    /// incoming messages here; the MCP entry point pops them.
    pub(crate) external_inbox: Option<ExternalAgentInbox>,
    /// Optional per-actor backend override. The python layer sets
    /// this when a persona's training adapter should route to a
    /// dedicated vLLM endpoint rather than the world's shared
    /// backend, so a trained persona and a frontier-served agent can
    /// coexist in the same scenario.
    pub(crate) backend_override: Option<BackendChoice>,
    /// User-only: when false, the UserActor records incoming messages
    /// into its history and returns without calling the backend. The
    /// actor can still drive the conversation via `.say()` from the
    /// scenario. Used for scripted personas whose only job is to
    /// deliver one or more seed messages and then stay silent.
    /// Defaults to true (the LLM-driven simulated user that responds
    /// to every agent reply).
    pub(crate) interactive: bool,
}

/// A resolved per-actor backend chosen by the python layer. Built into
/// a `SharedBackend` by `build_actor` at scheduler launch time.
#[derive(Clone, Debug)]
pub(crate) enum BackendChoice {
    Vllm { base_url: String, adapter: Option<String> },
}

#[derive(Clone, Copy, Eq, PartialEq, Debug)]
pub(crate) enum SpecKind {
    User,
    Agent,
    /// An agent slot driven from outside the process (e.g. via MCP).
    /// The actor's `step` forwards incoming messages into a shared
    /// queue the python layer drains; outbound messages and tool
    /// dispatches come back through `external_send_as` and
    /// `dispatch_as`.
    External,
}

/// A queue of messages addressed to an externally-driven agent slot.
/// `step` pushes; the python layer drains via `external_recv`.
#[derive(Clone, Default)]
pub(crate) struct ExternalAgentInbox {
    queue: Arc<Mutex<VecDeque<ExternalInboxItem>>>,
}

#[derive(Clone, Debug)]
pub(crate) struct ExternalInboxItem {
    pub from: String,
    pub kind: &'static str,
    pub text: String,
}

impl ExternalAgentInbox {
    fn new() -> Self {
        Self::default()
    }

    fn push(&self, item: ExternalInboxItem) {
        self.queue.lock().push_back(item);
    }

    fn pop(&self) -> Option<ExternalInboxItem> {
        self.queue.lock().pop_front()
    }
}

/// An actor that owns no LLM. Its only job is to forward inbound
/// messages into an [`ExternalAgentInbox`] so a process outside the
/// scheduler (the MCP-connected client) can react.
pub(crate) struct ExternalForwardActor {
    pub id: ActorId,
    pub inbox: ExternalAgentInbox,
}

#[async_trait]
impl ensemble_core::actor::Actor for ExternalForwardActor {
    fn id(&self) -> ActorId {
        self.id.clone()
    }
    fn kind(&self) -> ensemble_core::actor::ActorKind {
        ensemble_core::actor::ActorKind::Agent
    }
    async fn step(
        &self,
        _bus: &Bus,
        envelope: ensemble_core::bus::Envelope,
    ) -> Result<(), ensemble_core::error::CoreError> {
        let (kind, text) = match envelope.message {
            Message::UserMessage { text } => ("user", text),
            Message::AgentMessage { text } => ("agent", text),
            Message::ToolResult { name, result, is_error, .. } => {
                let prefix = if is_error { "tool_error" } else { "tool_result" };
                (
                    prefix,
                    format!("{} {}: {}", prefix, name, result),
                )
            }
            _ => return Ok(()),
        };
        self.inbox.push(ExternalInboxItem {
            from: envelope.from.to_string(),
            kind,
            text,
        });
        Ok(())
    }
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
        // Worlds are no longer registered in rust; the python layer
        // owns the plugin registry and calls register_tool /
        // register_predicate to populate this world after construction.
        // We still take a bundle so the default predicates are in place
        // before any plugin code runs.
        let bundle = WorldRegistry::build(&name).unwrap_or_else(empty_world_bundle);
        let script = MockScript::new();
        let (backend, kind) = build_backend(backend, base_url, &script)?;
        let log = EventLog::new();
        let bus = Bus::new(log.clone());
        // Real LLM round trips can easily blow past the 500ms default
        // quiescence window; bump it whenever the backend is anything
        // other than mock so the watcher does not abort an in-flight
        // HTTP call.
        let mut budget = TickBudget::default();
        // dispatch_async spawns the (sync) tool closure on the tokio
        // blocking pool and calls back into python under the GIL. The
        // round trip easily exceeds the 500ms default quiescence
        // window even with the mock backend, so bump it to 2s for
        // mock and to 60s for any real-network backend.
        budget.quiescence_ms = 2_000;
        if kind != "mock" {
            budget.quiescence_ms = 60_000;
        }
        // Allow an override for long-running blocking tool dispatches
        // (CUDA compiles, container starts) that routinely exceed the
        // default. Read once at world construction.
        if let Ok(env) = std::env::var("ENSEMBLE_QUIESCENCE_MS") {
            if let Ok(v) = env.parse::<u64>() {
                budget.quiescence_ms = v;
            }
        }
        let resources = Arc::new(ensemble_runtime::resources::shared(&name));
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
                resources,
                actors: vec![],
                seed_messages: vec![],
                budget,
                bg_task: None,
                registered_inboxes: vec![],
                external_inboxes: HashMap::new(),
            })),
        })
    }

    /// Name of the active backend (`"mock"`, `"anthropic"`,
    /// `"openai"`, `"vllm"`).
    #[getter]
    fn backend(&self) -> &'static str {
        self.inner.lock().backend_kind
    }

    #[getter]
    fn name(&self) -> String {
        self.inner.lock().name.clone()
    }

    /// Count of actors registered on this world.
    fn actor_count(&self) -> usize {
        self.inner.lock().actors.len()
    }

    #[pyo3(signature = (id=None, persona=None, hidden_goal=None, model="user-model", system_prompt=None, hidden_state_json=None, vllm_base_url=None, vllm_adapter=None, interactive=true))]
    fn spawn_user(
        &self,
        id: Option<&str>,
        persona: Option<&str>,
        hidden_goal: Option<&str>,
        model: &str,
        system_prompt: Option<&str>,
        hidden_state_json: Option<&str>,
        vllm_base_url: Option<&str>,
        vllm_adapter: Option<&str>,
        interactive: bool,
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
        let backend_override = vllm_base_url.map(|base| BackendChoice::Vllm {
            base_url: base.to_string(),
            adapter: vllm_adapter.map(str::to_string),
        });
        let spec = ActorSpec {
            id: actor_id.clone(),
            kind: SpecKind::User,
            persona: persona.map(str::to_string),
            hidden_goal: hidden_goal.map(str::to_string),
            model: Some(model.into()),
            tools: None,
            system_prompt: system_prompt.map(str::to_string),
            extra_params: Default::default(),
            hidden: hidden.clone(),
            external_inbox: None,
            backend_override: backend_override.clone(),
            interactive,
        };
        self.inner.lock().actors.push(spec);
        Ok(User {
            id: actor_id.to_string(),
            world: self.inner.clone(),
            hidden,
            backend_choice: backend_override,
        })
    }

    #[pyo3(signature = (id=None, model="claude-sonnet-4-5", tools=None, system_prompt=None, params_json=None))]
    fn spawn_agent(
        &self,
        id: Option<&str>,
        model: &str,
        tools: Option<&Bound<'_, PyList>>,
        system_prompt: Option<&str>,
        params_json: Option<&str>,
    ) -> PyResult<Agent> {
        let actor_id = ActorId::from_label(id.unwrap_or("agent"));
        let tool_names: Option<Vec<String>> = match tools {
            Some(list) => Some(
                list.iter()
                    .map(|item| item.extract::<String>())
                    .collect::<PyResult<_>>()?,
            ),
            None => None,
        };
        let extra_params: std::collections::HashMap<String, serde_json::Value> = match params_json {
            Some(s) if !s.is_empty() => {
                let v: serde_json::Value = serde_json::from_str(s)
                    .map_err(|e| PyValueError::new_err(format!("params_json: {e}")))?;
                match v {
                    serde_json::Value::Object(map) => map.into_iter().collect(),
                    serde_json::Value::Null => Default::default(),
                    _ => return Err(PyValueError::new_err("params_json must encode a JSON object")),
                }
            }
            _ => Default::default(),
        };
        let spec = ActorSpec {
            id: actor_id.clone(),
            kind: SpecKind::Agent,
            persona: None,
            hidden_goal: None,
            model: Some(model.into()),
            tools: tool_names,
            system_prompt: system_prompt.map(str::to_string),
            extra_params,
            hidden: None,
            external_inbox: None,
            backend_override: None,
            interactive: true,
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
    fn run_until(&self, py: Python<'_>, until_spec_json: &str) -> PyResult<()> {
        let spec: serde_json::Value = serde_json::from_str(until_spec_json)
            .map_err(|e| PyValueError::new_err(format!("bad until spec: {e}")))?;
        let until = build_until(&spec)?;
        let (bus, actor_handles, seed_messages, budget, predicates) = {
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
            let predicates = inner.predicates.clone();
            let mut handles: Vec<(ActorId, Arc<dyn ensemble_core::actor::Actor>)> = Vec::new();
            for spec in inner.actors.drain(..) {
                let actor = build_actor(spec, backend.clone(), tools.clone());
                handles.push(actor);
            }
            inner.registered_inboxes = handles.iter().map(|(id, _)| id.clone()).collect();
            let seed = inner.seed_messages.drain(..).collect::<Vec<_>>();
            // Loud no-inbox guard. The single biggest "scheduler
            // quiesced; halting" trap is a scenario that spawns an
            // agent without ever calling .say() or spawning a user
            // who does. The scheduler then runs for one quiescence
            // window with nothing to deliver and exits with an empty
            // trace. Log a structured note so the trace viewer (and
            // anyone reading the JSONL) can see the cause; the run
            // still proceeds in case the scenario is intentionally
            // empty.
            if seed.is_empty() {
                let agents_only = handles
                    .iter()
                    .all(|(_, a)| matches!(a.kind(), ensemble_core::actor::ActorKind::Agent));
                if agents_only && !handles.is_empty() {
                    let bus_for_warn = bus.clone();
                    global_runtime().block_on(bus_for_warn.append_event(
                        None,
                        ensemble_core::event::EventPayload::System {
                            note: "ensemble: no seed messages queued and only agents are \
                                   registered; the scheduler will quiesce on the first \
                                   tick. Spawn a User and call .say(...), or call \
                                   agent.say(...) to kick off the conversation."
                                .into(),
                        },
                    ));
                    eprintln!(
                        "ensemble: no seed messages queued and only agents are registered. \
                         The scheduler will quiesce on the first tick. Spawn a User and call \
                         .say(...), or call agent.say(...) to kick off the conversation."
                    );
                }
            }
            (bus, handles, seed, budget, predicates)
        };

        let runtime = global_runtime();
        // Release the GIL across the scheduler run so plugin tools and
        // predicates implemented in python can call back into the
        // interpreter without deadlocking on this thread's lock.
        py.detach(|| {
            runtime
                .block_on(async move {
                    let mut scheduler = Scheduler::new(bus.clone(), budget)
                        .with_predicates(predicates.clone());
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
        })
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

    /// Mirror every event to a JSONL file as it is appended. Pass
    /// `None` to detach an existing sink. Useful for watching a long
    /// run in real time (`tail -f traces/foo.jsonl | jq`) and for
    /// publishing intermediate state to a viewer that polls the file.
    #[pyo3(signature = (path=None))]
    fn set_trace_path(&self, path: Option<&str>) -> PyResult<()> {
        let log = self.inner.lock().log.clone();
        let runtime = global_runtime();
        match path {
            Some(p) => {
                let p = p.to_string();
                runtime
                    .block_on(async move {
                        let sink = TraceFile::create(&p).await?;
                        log.set_sink(Some(sink)).await;
                        Ok::<_, std::io::Error>(())
                    })
                    .map_err(|e| PyRuntimeError::new_err(format!("trace sink: {e}")))?;
            }
            None => {
                runtime.block_on(async move {
                    log.set_sink(None).await;
                });
            }
        }
        Ok(())
    }

    /// Current trace sink path, if any.
    fn trace_path(&self) -> PyResult<Option<String>> {
        let log = self.inner.lock().log.clone();
        let runtime = global_runtime();
        Ok(runtime
            .block_on(log.sink_path())
            .map(|p| p.to_string_lossy().into_owned()))
    }

    /// Start the scheduler in the background on the global tokio
    /// runtime. Returns immediately. The caller is expected to drive
    /// it via `wait_for_until` and stop it via `stop_scheduler`.
    fn start_scheduler(&self) -> PyResult<()> {
        let (bus, actor_handles, seed_messages, mut budget, predicates) = {
            let mut inner = self.inner.lock();
            if inner.bg_task.is_some() {
                return Err(PyRuntimeError::new_err("scheduler already started"));
            }
            let backend = inner.backend.clone();
            let tools = inner.tools.clone();
            let bus = inner.bus.clone();
            let predicates = inner.predicates.clone();
            let mut handles: Vec<(ActorId, Arc<dyn ensemble_core::actor::Actor>)> = Vec::new();
            for spec in inner.actors.drain(..) {
                let actor = build_actor(spec, backend.clone(), tools.clone());
                handles.push(actor);
            }
            let seed = inner.seed_messages.drain(..).collect::<Vec<_>>();
            (bus, handles, seed, inner.budget, predicates)
        };
        // Loosen the budget: the python harness is in charge of when
        // to stop. Still keep quiescence so a stalled run halts.
        budget.max_ticks = budget.max_ticks.max(10_000);
        budget.max_events = budget.max_events.max(50_000);
        budget.quiescence_ms = budget.quiescence_ms.max(2_000);

        let runtime = global_runtime();
        let mut registered = Vec::new();
        let _enter = runtime.enter();
        let mut scheduler = Scheduler::new(bus.clone(), budget).with_predicates(predicates);
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
    fn wait_for_until(
        &self,
        py: Python<'_>,
        until_spec_json: &str,
        timeout_ms: u64,
    ) -> PyResult<bool> {
        let spec: serde_json::Value = serde_json::from_str(until_spec_json)
            .map_err(|e| PyValueError::new_err(format!("bad until spec: {e}")))?;
        let until = build_until(&spec)?;
        let bus = self.inner.lock().bus.clone();
        let log = self.inner.lock().log.clone();
        let predicates = self.inner.lock().predicates.clone();
        let runtime = global_runtime();
        let notifier = bus.notifier();
        let deadline = std::time::Instant::now() + std::time::Duration::from_millis(timeout_ms);

        Ok(py.detach(|| {
            runtime.block_on(async move {
                loop {
                    let cur = log.len().await as u64;
                    let snapshot = log.snapshot().await;
                    let preds = Some(&predicates);
                    let ctx = UntilCtx {
                        tick: cur,
                        log: &log,
                        events_seen: cur as usize,
                        trace: Some(snapshot.as_slice()),
                        predicates: preds,
                    };
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
            })
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

    /// Names of the tools registered on this world's `ToolRegistry`.
    fn tool_names(&self) -> Vec<String> {
        self.inner.lock().tools.names()
    }

    /// Declare a named resource on this world's `ResourceManager`.
    /// `permits = 1` is an exclusive lock (the lazy default); values
    /// greater than one declare a shared resource with that many
    /// simultaneous holders. Idempotent: a second declaration with
    /// the same name keeps the existing permit count, since changing
    /// capacity mid-run would break callers already holding permits.
    /// The python `World` constructor calls this for each entry in
    /// the manifest's resources table so a `world.toml`-declared
    /// `Shared{permits: 2}` actually serves two concurrent tool
    /// dispatches rather than silently downgrading to exclusive.
    fn declare_resource(&self, name: &str, permits: u32) {
        let resources = self.inner.lock().resources.clone();
        let kind = if permits <= 1 {
            ensemble_runtime::ResourceKind::Exclusive
        } else {
            ensemble_runtime::ResourceKind::Shared { permits }
        };
        resources.declare(name.to_string(), kind);
    }

    /// Resource names known to this world's manager: anything
    /// declared via :meth:`declare_resource` plus anything tools
    /// have acquired so far (those are lazily declared as
    /// exclusive).
    fn resource_names(&self) -> Vec<String> {
        self.inner.lock().resources.names()
    }

    /// Declare a budget cap for `unit`. When a recorded cost would
    /// push the running total past `amount` the scheduler halts with
    /// StopReason::BudgetExceeded. When `actor` is supplied the cap
    /// is scoped to that actor's own running total; otherwise it is
    /// a world-wide cap.
    #[pyo3(signature = (unit, amount, actor=None))]
    fn set_budget(
        &self,
        py: Python<'_>,
        unit: &str,
        amount: f64,
        actor: Option<&str>,
    ) {
        let bus = self.inner.lock().bus.clone();
        let unit = unit.to_string();
        let actor = actor.map(ActorId::from_label);
        py.detach(|| {
            global_runtime().block_on(async move {
                bus.set_budget(unit, amount, actor).await;
            })
        });
    }

    /// Read the running total for `unit`. When `actor` is supplied,
    /// returns the actor's own running total; otherwise the
    /// world-wide total. 0.0 if nothing has been recorded yet.
    #[pyo3(signature = (unit, actor=None))]
    fn cost_total(&self, py: Python<'_>, unit: &str, actor: Option<&str>) -> f64 {
        let bus = self.inner.lock().bus.clone();
        let unit = unit.to_string();
        let actor = actor.map(ActorId::from_label);
        py.detach(|| {
            global_runtime().block_on(async move {
                match actor {
                    Some(a) => bus.actor_cost_total(&a, &unit).await,
                    None => bus.cost_total(&unit).await,
                }
            })
        })
    }

    /// Record a cost annotation outside of a tool dispatch (tests,
    /// backend-side reporting from python). Emits a `Cost` event,
    /// bumps the running total, and halts the scheduler if a budget
    /// is now exceeded.
    #[pyo3(signature = (unit, amount, actor=None))]
    fn record_cost(
        &self,
        py: Python<'_>,
        unit: &str,
        amount: f64,
        actor: Option<&str>,
    ) -> PyResult<()> {
        let bus = self.inner.lock().bus.clone();
        let unit = unit.to_string();
        let actor = actor.map(ActorId::from_label);
        py.detach(|| {
            global_runtime()
                .block_on(async move { bus.record_cost(unit, amount, actor).await })
        });
        Ok(())
    }

    /// Register an externally-driven agent slot. The scheduler-spawned
    /// actor for this id will forward incoming messages into a queue
    /// the python layer drains via `external_recv`. Used by the MCP
    /// entry point to plumb scenario messages to a connected client.
    #[pyo3(signature = (id, tools=None))]
    fn register_external_agent(
        &self,
        id: &str,
        tools: Option<&Bound<'_, PyList>>,
    ) -> PyResult<()> {
        let actor_id = ActorId::from_label(id);
        let tool_names: Option<Vec<String>> = match tools {
            Some(list) => Some(
                list.iter()
                    .map(|item| item.extract::<String>())
                    .collect::<PyResult<_>>()?,
            ),
            None => None,
        };
        let inbox = ExternalAgentInbox::new();
        {
            let mut inner = self.inner.lock();
            inner
                .external_inboxes
                .insert(actor_id.clone(), inbox.clone());
            inner.actors.push(ActorSpec {
                id: actor_id,
                kind: SpecKind::External,
                persona: None,
                hidden_goal: None,
                model: None,
                tools: tool_names,
                system_prompt: None,
                extra_params: Default::default(),
                hidden: None,
                external_inbox: Some(inbox),
                backend_override: None,
                interactive: true,
            });
        }
        Ok(())
    }

    /// Pop the next message addressed to an external agent. Returns a
    /// dict with `from`, `kind`, and `text` or `None` when nothing is
    /// pending. Polling-based; callers add their own sleep loop.
    fn external_recv(&self, py: Python<'_>, agent_id: &str) -> PyResult<Py<PyAny>> {
        let key = ActorId::from_label(agent_id);
        let item = {
            let inner = self.inner.lock();
            let Some(inbox) = inner.external_inboxes.get(&key) else {
                return Err(PyValueError::new_err(format!(
                    "no external agent registered as {agent_id:?}"
                )));
            };
            inbox.pop()
        };
        match item {
            None => Ok(py.None()),
            Some(item) => {
                let d = pyo3::types::PyDict::new(py);
                d.set_item("from", item.from)?;
                d.set_item("kind", item.kind)?;
                d.set_item("text", item.text)?;
                Ok(d.into())
            }
        }
    }

    /// Send a message from an external agent slot to a target actor.
    /// Goes through the bus and ends up in the trace as the agent
    /// having spoken.
    fn external_send_as(
        &self,
        py: Python<'_>,
        agent_id: &str,
        target: &str,
        text: &str,
    ) -> PyResult<()> {
        let bus = self.inner.lock().bus.clone();
        let msg = Message::AgentMessage { text: text.into() };
        py.detach(|| {
            global_runtime().block_on(bus.send(
                ActorId::from_label(agent_id),
                Recipient::Actor(ActorId::from_label(target)),
                msg,
            ))
        })
        .map_err(|e| PyRuntimeError::new_err(format!("send: {e}")))
    }

    /// Dispatch a tool as if `agent_id` had issued it: emits paired
    /// ToolCall + ToolResult events (and a StateDiff if any) attributed
    /// to that actor. Returns the JSON body the MCP server hands back
    /// to the client.
    fn dispatch_as(
        &self,
        py: Python<'_>,
        agent_id: &str,
        tool_name: &str,
        args_json: &str,
    ) -> PyResult<String> {
        let args: serde_json::Value = serde_json::from_str(args_json)
            .map_err(|e| PyValueError::new_err(format!("bad args json: {e}")))?;
        let (bus, tools, resources) = {
            let inner = self.inner.lock();
            (
                inner.bus.clone(),
                inner.tools.clone(),
                inner.resources.clone(),
            )
        };
        let actor = ActorId::from_label(agent_id);
        let call_id = MessageId::new().to_string();
        let tool_owned = tool_name.to_string();
        let runtime = global_runtime();
        
        let outcome_json = py.detach(move || {
            runtime.block_on(async move {
                bus.append_event(
                    Some(actor.clone()),
                    EventPayload::ToolCall {
                        id: call_id.clone(),
                        name: tool_owned.clone(),
                        args: args.clone(),
                        seed: false,
                    },
                )
                .await;
                let dispatch = tools
                    .dispatch_async(&tool_owned, &args, Some(&resources))
                    .await;
                for entry in dispatch.progress {
                    bus.append_event(
                        Some(actor.clone()),
                        EventPayload::Progress {
                            id: call_id.clone(),
                            tool: tool_owned.clone(),
                            fraction: entry.fraction,
                            message: entry.message,
                        },
                    )
                    .await;
                }
                if let Some(after) = dispatch.timed_out_after {
                    bus.append_event(
                        Some(actor.clone()),
                        EventPayload::ToolTimeout {
                            id: call_id.clone(),
                            name: tool_owned.clone(),
                            after_ms: after.as_millis() as u64,
                        },
                    )
                    .await;
                }
                let mut body = serde_json::Map::new();
                match dispatch.outcome {
                    Ok(outcome) => {
                        body.insert("effect".into(), outcome.effect.clone());
                        if let Some(diff) = outcome.diff.clone() {
                            body.insert("diff".into(), diff);
                        }
                        let costs = outcome.costs.clone();
                        bus.append_event(
                            Some(actor.clone()),
                            EventPayload::ToolResult {
                                id: call_id.clone(),
                                name: tool_owned.clone(),
                                result: outcome.effect,
                                is_error: false,
                                seed: false,
                            },
                        )
                        .await;
                        if let Some(diff) = outcome.diff {
                            bus.append_event(
                                Some(actor.clone()),
                                EventPayload::StateDiff { diff, seed: false },
                            )
                            .await;
                        }
                        for (unit, amount) in costs {
                            bus.record_cost(unit, amount, Some(actor.clone())).await;
                        }
                    }
                    Err(e) => {
                        let err_json =
                            serde_json::json!({"ok": false, "error": e.to_string()});
                        body.insert("effect".into(), err_json.clone());
                        body.insert("is_error".into(), serde_json::Value::Bool(true));
                        bus.append_event(
                            Some(actor),
                            EventPayload::ToolResult {
                                id: call_id,
                                name: tool_owned,
                                result: err_json,
                                is_error: true,
                                seed: false,
                            },
                        )
                        .await;
                    }
                }
                serde_json::Value::Object(body).to_string()
            })
        });
        Ok(outcome_json)
    }

    /// World-level apply: invoke a tool as a system mutation, with
    /// no actor attribution. The python equivalent of the Rust
    /// `WorldHandle::apply_and_log` path: appends a `ToolCall`, runs
    /// the registered tool, and appends `ToolResult` plus an optional
    /// `StateDiff` to the trace. Used by python world authors who
    /// want to evolve world state outside of an actor's turn (test
    /// setup, scenario-author seed actions that don't belong to any
    /// user, scheduled world events). Returns the effect JSON.
    fn apply(
        &self,
        py: Python<'_>,
        tool_name: &str,
        args_json: &str,
    ) -> PyResult<String> {
        let args: serde_json::Value = serde_json::from_str(args_json)
            .map_err(|e| PyValueError::new_err(format!("bad args json: {e}")))?;
        let (bus, tools, resources) = {
            let inner = self.inner.lock();
            (
                inner.bus.clone(),
                inner.tools.clone(),
                inner.resources.clone(),
            )
        };
        let call_id = MessageId::new().to_string();
        let tool_owned = tool_name.to_string();
        let runtime = global_runtime();

        let outcome_json = py.detach(move || {
            runtime.block_on(async move {
                bus.append_event(
                    None,
                    EventPayload::ToolCall {
                        id: call_id.clone(),
                        name: tool_owned.clone(),
                        args: args.clone(),
                        seed: true,
                    },
                )
                .await;
                let dispatch = tools
                    .dispatch_async(&tool_owned, &args, Some(&resources))
                    .await;
                for entry in dispatch.progress {
                    bus.append_event(
                        None,
                        EventPayload::Progress {
                            id: call_id.clone(),
                            tool: tool_owned.clone(),
                            fraction: entry.fraction,
                            message: entry.message,
                        },
                    )
                    .await;
                }
                if let Some(after) = dispatch.timed_out_after {
                    bus.append_event(
                        None,
                        EventPayload::ToolTimeout {
                            id: call_id.clone(),
                            name: tool_owned.clone(),
                            after_ms: after.as_millis() as u64,
                        },
                    )
                    .await;
                }
                let mut body = serde_json::Map::new();
                match dispatch.outcome {
                    Ok(outcome) => {
                        body.insert("effect".into(), outcome.effect.clone());
                        if let Some(diff) = outcome.diff.clone() {
                            body.insert("diff".into(), diff);
                        }
                        let costs = outcome.costs.clone();
                        bus.append_event(
                            None,
                            EventPayload::ToolResult {
                                id: call_id.clone(),
                                name: tool_owned.clone(),
                                result: outcome.effect,
                                is_error: false,
                                seed: true,
                            },
                        )
                        .await;
                        if let Some(diff) = outcome.diff {
                            bus.append_event(
                                None,
                                EventPayload::StateDiff { diff, seed: true },
                            )
                            .await;
                        }
                        for (unit, amount) in costs {
                            bus.record_cost(unit, amount, None).await;
                        }
                    }
                    Err(e) => {
                        let err_json =
                            serde_json::json!({"ok": false, "error": e.to_string()});
                        body.insert("effect".into(), err_json.clone());
                        body.insert("is_error".into(), serde_json::Value::Bool(true));
                        bus.append_event(
                            None,
                            EventPayload::ToolResult {
                                id: call_id,
                                name: tool_owned,
                                result: err_json,
                                is_error: true,
                                seed: true,
                            },
                        )
                        .await;
                    }
                }
                serde_json::Value::Object(body).to_string()
            })
        });
        Ok(outcome_json)
    }

    /// Register a python-callable tool. The callable receives the
    /// args JSON as a string and must return a JSON string of either
    /// `{"effect": ...}` or `{"effect": ..., "diff": ...}`. The diff,
    /// when present, is emitted as a StateDiff event after the
    /// ToolResult.
    #[pyo3(signature = (name, description, parameters_json, callable, timeout_ms=None, resources=None))]
    fn register_tool(
        &self,
        name: &str,
        description: &str,
        parameters_json: &str,
        callable: Py<PyAny>,
        timeout_ms: Option<u64>,
        resources: Option<Vec<String>>,
    ) -> PyResult<()> {
        let parameters: serde_json::Value = serde_json::from_str(parameters_json)
            .map_err(|e| PyValueError::new_err(format!("bad parameters json: {e}")))?;
        let tools = self.inner.lock().tools.clone();
        let tool_name = name.to_string();
        let wrapper = move |args: &serde_json::Value, emitter: &ensemble_runtime::ProgressEmitter|
            -> Result<ToolOutcome, ToolError> {
            let args_str = serde_json::to_string(args)
                .map_err(|e| ToolError::Execution(format!("serialize args: {e}")))?;
            Python::attach(|py| {
                let result_obj = callable
                    .call1(py, (args_str,))
                    .map_err(|e| ToolError::Execution(format!("python tool: {e}")))?;
                let result_str: String = result_obj.extract(py).map_err(|e| {
                    ToolError::Execution(format!(
                        "python tool must return a JSON string: {e}"
                    ))
                })?;
                let parsed: serde_json::Value = serde_json::from_str(&result_str)
                    .map_err(|e| ToolError::Execution(format!(
                        "python tool returned non-json: {e}"
                    )))?;
                let effect = parsed
                    .get("effect")
                    .cloned()
                    .unwrap_or(serde_json::Value::Null);
                let diff = parsed.get("diff").cloned();
                let mut costs: std::collections::HashMap<String, f64> = Default::default();
                if let Some(serde_json::Value::Object(map)) = parsed.get("costs") {
                    for (k, v) in map {
                        if let Some(n) = v.as_f64() {
                            costs.insert(k.clone(), n);
                        }
                    }
                }
                // Forward any progress entries the python tool emitted
                // (via the {"progress": [...]} key) into the outer
                // ProgressEmitter so the runtime can flush them to
                // the trace.
                if let Some(serde_json::Value::Array(arr)) = parsed.get("progress") {
                    for entry in arr {
                        let fraction = entry
                            .get("fraction")
                            .and_then(|v| v.as_f64())
                            .unwrap_or(0.0) as f32;
                        let message = entry
                            .get("message")
                            .and_then(|v| v.as_str())
                            .unwrap_or("")
                            .to_string();
                        emitter.emit(fraction, message);
                    }
                }
                Ok(ToolOutcome { effect, diff, costs })
            })
        };
        tools.register(Tool {
            schema: ensemble_runtime::ToolSchema {
                name: tool_name,
                description: description.to_string(),
                parameters,
            },
            timeout: timeout_ms.map(std::time::Duration::from_millis),
            resources: resources.unwrap_or_default(),
            run: Arc::new(wrapper),
        });
        Ok(())
    }

    /// Register a python-callable predicate. The callable receives
    /// `(trace_json: str, args_json: str)` and must return a bool.
    /// `trace_json` is the full event log at evaluation time.
    fn register_predicate(&self, name: &str, callable: Py<PyAny>) -> PyResult<()> {
        let preds = self.inner.lock().predicates.clone();
        preds.register(name.to_string(), move |ctx| {
            let trace_str =
                serde_json::to_string(ctx.trace).unwrap_or_else(|_| "[]".into());
            let args_str = ctx.args.to_string();
            Python::attach(|py| {
                match callable.call1(py, (trace_str, args_str)) {
                    Ok(v) => v.extract::<bool>(py).unwrap_or(false),
                    Err(e) => {
                        // Surface as `false` rather than panic; the
                        // python exception is printed to stderr so it
                        // is visible during scenario runs.
                        e.print(py);
                        false
                    }
                }
            })
        });
        Ok(())
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
    backend_choice: Option<BackendChoice>,
}

#[pymethods]
impl User {
    /// Resolved per-user backend, if any. Returns a dict with the
    /// chosen kind and parameters (e.g. `{"kind": "vllm", "base_url":
    /// "...", "adapter": "..."}`) when the persona's training
    /// adapter routed this user to a dedicated endpoint; returns
    /// `None` when the user shares the world's default backend. Used
    /// by tests and tooling to verify the resolved choice without
    /// having to stand up the backend itself.
    fn backend_info(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        match &self.backend_choice {
            None => Ok(py.None()),
            Some(BackendChoice::Vllm { base_url, adapter }) => {
                let d = pyo3::types::PyDict::new(py);
                d.set_item("kind", "vllm")?;
                d.set_item("base_url", base_url)?;
                d.set_item("adapter", adapter.clone())?;
                Ok(d.into())
            }
        }
    }

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
    fn act_json(&self, py: Python<'_>, tool: &str, args_json: &str) -> PyResult<()> {
        let args: serde_json::Value = serde_json::from_str(args_json)
            .map_err(|e| PyValueError::new_err(format!("bad json: {e}")))?;
        let call_id = MessageId::new().to_string();
        let (bus, tools, resources) = {
            let inner = self.world.lock();
            (
                inner.bus.clone(),
                inner.tools.clone(),
                inner.resources.clone(),
            )
        };
        let actor = ActorId::from_label(&self.id);
        let tool_owned = tool.to_string();
        let runtime = global_runtime();
        
        // Plugin tools call back into Python under the GIL; the
        // spawn_blocking path inside dispatch_async will deadlock if
        // we hold the GIL through block_on. Release it for the
        // duration of the dispatch.
        py.detach(|| runtime.block_on(async move {
            bus.append_event(
                Some(actor.clone()),
                EventPayload::ToolCall {
                    id: call_id.clone(),
                    name: tool_owned.clone(),
                    args: args.clone(),
                    seed: true,
                },
            )
            .await;
            let dispatch = tools.dispatch_async(&tool_owned, &args, Some(&resources)).await;
            for entry in dispatch.progress {
                bus.append_event(
                    Some(actor.clone()),
                    EventPayload::Progress {
                        id: call_id.clone(),
                        tool: tool_owned.clone(),
                        fraction: entry.fraction,
                        message: entry.message,
                    },
                )
                .await;
            }
            if let Some(after) = dispatch.timed_out_after {
                bus.append_event(
                    Some(actor.clone()),
                    EventPayload::ToolTimeout {
                        id: call_id.clone(),
                        name: tool_owned.clone(),
                        after_ms: after.as_millis() as u64,
                    },
                )
                .await;
            }
            match dispatch.outcome {
                Ok(outcome) => {
                    let costs = outcome.costs.clone();
                    bus.append_event(
                        Some(actor.clone()),
                        EventPayload::ToolResult {
                            id: call_id,
                            name: tool_owned,
                            result: outcome.effect,
                            is_error: false,
                            seed: true,
                        },
                    )
                    .await;
                    if let Some(diff) = outcome.diff {
                        bus.append_event(
                            Some(actor.clone()),
                            EventPayload::StateDiff { diff, seed: true },
                        )
                        .await;
                    }
                    for (unit, amount) in costs {
                        bus.record_cost(unit, amount, Some(actor.clone())).await;
                    }
                }
                Err(e) => {
                    let err_json = serde_json::json!({"ok": false, "error": e.to_string()});
                    bus.append_event(
                        Some(actor),
                        EventPayload::ToolResult {
                            id: call_id,
                            name: tool_owned,
                            result: err_json,
                            is_error: true,
                            seed: true,
                        },
                    )
                    .await;
                }
            }
        }));
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
/// HiddenState, the chosen backend is wrapped in a `PromptedPersona`
/// so the model sees the persona's hidden state on every turn. A
/// `backend_override` (set by the python layer for a trained
/// persona) replaces the world-shared backend before that wrapping;
/// the persona's system prompt then layers on top of a vLLM-served
/// adapter rather than the world default.
fn build_actor(
    spec: ActorSpec,
    backend: SharedBackend,
    tools: Arc<ToolRegistry>,
) -> (ActorId, Arc<dyn ensemble_core::actor::Actor>) {
    let id = spec.id.clone();
    let model = spec.model.clone().unwrap_or_else(|| match spec.kind {
        SpecKind::User => "user-model".into(),
        SpecKind::Agent => "agent-model".into(),
        SpecKind::External => "external-agent".into(),
    });
    let base_backend: SharedBackend = match &spec.backend_override {
        Some(BackendChoice::Vllm { base_url, adapter }) => {
            let mut be = LocalAdapterBackend::new(base_url);
            if let Some(adapter) = adapter {
                be = be.with_adapter(adapter);
            }
            Arc::new(be)
        }
        None => backend,
    };
    let backend_for_actor: SharedBackend = match (&spec.system_prompt, &spec.hidden) {
        (Some(template), Some(hidden)) => Arc::new(PromptedPersona::new(
            base_backend,
            model.clone(),
            template.clone(),
            hidden.clone(),
        )),
        _ => base_backend,
    };
    let actor: Arc<dyn ensemble_core::actor::Actor> = match spec.kind {
        SpecKind::User => {
            let mut a = UserActor::new(spec.id, model, backend_for_actor)
                .with_interactive(spec.interactive);
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
            if let Some(allowed) = spec.tools {
                a = a.with_allowed_tools(allowed);
            }
            if !spec.extra_params.is_empty() {
                a = a.with_extra_params(spec.extra_params);
            }
            Arc::new(a)
        }
        SpecKind::External => {
            // The inbox is populated by register_external_agent; if it
            // ever isn't, fall back to a fresh inbox (the python layer
            // will never see those messages but the scheduler keeps
            // running).
            let inbox = spec.external_inbox.unwrap_or_default();
            Arc::new(ExternalForwardActor { id: spec.id, inbox })
        }
    };
    (id, actor)
}

fn build_backend(
    name: Option<&str>,
    base_url: Option<&str>,
    script: &MockScript,
) -> PyResult<(SharedBackend, &'static str)> {
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
        "mock" => (Arc::new(MockBackend::new(script.clone())) as SharedBackend, "mock"),
        "anthropic" => {
            let mut be = AnthropicBackend::from_env()
                .map_err(|e| PyValueError::new_err(format!("{e}")))?;
            let url = base_url
                .map(str::to_string)
                .or_else(|| std::env::var("ANTHROPIC_BASE_URL").ok());
            if let Some(url) = url {
                be = be.with_base_url(url);
            }
            (Arc::new(be) as SharedBackend, "anthropic")
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
            (Arc::new(be) as SharedBackend, "openai")
        }
        "vllm" => {
            let base = base_url.ok_or_else(|| {
                PyValueError::new_err("vllm backend requires base_url=...")
            })?;
            let be = LocalAdapterBackend::new(base);
            (Arc::new(be) as SharedBackend, "vllm")
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
/// kinds: `turn_count_gt`, `turn_count_ge`, `predicate`, `any_of`,
/// `all_of`.
///
/// The `predicate` kind names a world-registered predicate. The
/// scheduler hands the closure an `UntilCtx` whose `predicates` field
/// is the world's `PredicateRegistry` and whose `trace` field is the
/// trace snapshot for this tick; the closure evaluates the named
/// predicate against that snapshot. Composes with the turn-count
/// kinds via `any_of` / `all_of`, so "stop on submit OR after N
/// turns" is one expression.
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
        "predicate" => {
            let name = spec
                .get("name")
                .and_then(|v| v.as_str())
                .ok_or_else(|| PyValueError::new_err("predicate until requires 'name'"))?
                .to_string();
            let args = spec.get("args").cloned().unwrap_or(serde_json::Value::Null);
            let label = format!("predicate({name})");
            Ok(Until::new(label, move |ctx: &UntilCtx<'_>| {
                let Some(preds) = ctx.predicates else {
                    return false;
                };
                let trace = ctx.trace.unwrap_or(&[]);
                let pctx = PredicateCtx::with_args(trace, args.clone());
                preds.evaluate(&name, &pctx).unwrap_or(false)
            }))
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
    WorldRegistry::register("noop", empty_world_bundle);

    m.add_class::<World>()?;
    m.add_class::<User>()?;
    m.add_class::<Agent>()?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
