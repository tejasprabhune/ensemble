use std::sync::Arc;

use once_cell::sync::Lazy;
use parking_lot::Mutex;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyList;

use ensemble_core::actor::ActorHandle;
use ensemble_core::bus::{Bus, Message, Recipient};
use ensemble_core::event::{EventLog, EventPayload};
use ensemble_core::ids::ActorId;
use ensemble_core::scheduler::{Scheduler, TickBudget};
use ensemble_core::until::{turn_count_exceeds, Until, UntilCtx};
use ensemble_runtime::{
    AgentActor, MockBackend, MockScript, MockTurn, ToolRegistry, UserActor,
};

mod world_registry;
use world_registry::{WorldBundle, WorldRegistry};

fn noop_world_builder() -> WorldBundle {
    WorldBundle { tools: ToolRegistry::new() }
}

fn plank_world_builder() -> WorldBundle {
    let (_state, tools) = plank::build();
    // The state Arc lives on inside each tool closure; tools holds it
    // alive for as long as the world instance does.
    WorldBundle { tools }
}

/// Inner world state shared between `World`, `User`, and `Agent`.
/// Actor specs and seed messages accumulate here and are consumed at
/// `run()` time (wired up in a later commit).
pub(crate) struct WorldInner {
    pub(crate) name: String,
    pub(crate) bus: Bus,
    pub(crate) log: EventLog,
    #[allow(dead_code)]
    pub(crate) backend: Arc<MockBackend>,
    pub(crate) script: MockScript,
    #[allow(dead_code)]
    pub(crate) tools: Arc<ToolRegistry>,
    pub(crate) actors: Vec<ActorSpec>,
    pub(crate) seed_messages: Vec<(ActorId, ActorId, Message)>,
    pub(crate) budget: TickBudget,
    pub(crate) bg_task: Option<tokio::task::JoinHandle<Result<(), ensemble_core::error::CoreError>>>,
    pub(crate) registered_inboxes: Vec<ActorId>,
}

#[derive(Clone)]
pub(crate) struct ActorSpec {
    pub(crate) id: ActorId,
    pub(crate) kind: SpecKind,
    pub(crate) persona: Option<String>,
    pub(crate) hidden_goal: Option<String>,
    pub(crate) model: Option<String>,
    pub(crate) tools: Vec<String>,
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
    #[new]
    #[pyo3(signature = (name=None))]
    fn new(name: Option<&str>) -> PyResult<Self> {
        let name = name.unwrap_or("noop").to_string();
        let bundle = WorldRegistry::build(&name).ok_or_else(|| {
            PyValueError::new_err(format!(
                "no world named {name:?}; register one before constructing it"
            ))
        })?;
        let script = MockScript::new();
        let backend = Arc::new(MockBackend::new(script.clone()));
        let log = EventLog::new();
        let bus = Bus::new(log.clone());
        Ok(Self {
            inner: Arc::new(Mutex::new(WorldInner {
                name,
                bus,
                log,
                backend,
                script,
                tools: Arc::new(bundle.tools),
                actors: vec![],
                seed_messages: vec![],
                budget: TickBudget::default(),
                bg_task: None,
                registered_inboxes: vec![],
            })),
        })
    }

    #[getter]
    fn name(&self) -> String {
        self.inner.lock().name.clone()
    }

    /// Count of actors registered on this world.
    fn actor_count(&self) -> usize {
        self.inner.lock().actors.len()
    }

    #[pyo3(signature = (id=None, persona=None, hidden_goal=None, model="user-model"))]
    fn spawn_user(
        &self,
        id: Option<&str>,
        persona: Option<&str>,
        hidden_goal: Option<&str>,
        model: &str,
    ) -> User {
        let actor_id = ActorId::from_label(id.unwrap_or_else(|| persona.unwrap_or("user")));
        let spec = ActorSpec {
            id: actor_id.clone(),
            kind: SpecKind::User,
            persona: persona.map(str::to_string),
            hidden_goal: hidden_goal.map(str::to_string),
            model: Some(model.into()),
            tools: vec![],
        };
        self.inner.lock().actors.push(spec);
        User {
            id: actor_id.to_string(),
            world: self.inner.clone(),
        }
    }

    #[pyo3(signature = (id=None, model="claude-sonnet-4-5", tools=None))]
    fn spawn_agent(
        &self,
        id: Option<&str>,
        model: &str,
        tools: Option<&Bound<'_, PyList>>,
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

    /// Test-only: queue a canned mock tool call for a given model.
    fn _mock_tool(&self, model: &str, tool: &str, args_json: &str) -> PyResult<()> {
        let args: serde_json::Value = serde_json::from_str(args_json)
            .map_err(|e| PyValueError::new_err(format!("bad json: {e}")))?;
        self.inner.lock().script.push_for(model, MockTurn::tool(tool, args));
        Ok(())
    }

    /// Build and run the scheduler synchronously. `until_spec` is a
    /// JSON spec like `{"kind":"turn_count_gt","n":30}`. Blocks the
    /// calling thread until the scheduler stops.
    fn run_until(&self, until_spec_json: &str) -> PyResult<()> {
        let spec: serde_json::Value = serde_json::from_str(until_spec_json)
            .map_err(|e| PyValueError::new_err(format!("bad until spec: {e}")))?;
        let until = build_until(&spec)?;
        let (bus, actor_handles, seed_messages, budget) = {
            let mut inner = self.inner.lock();
            let backend = inner.backend.clone() as Arc<dyn ensemble_runtime::LLMBackend>;
            let tools = inner.tools.clone();
            let bus = inner.bus.clone();
            let budget = inner.budget;
            let mut handles: Vec<(ActorId, Arc<dyn ensemble_core::actor::Actor>)> = Vec::new();
            for spec in inner.actors.drain(..) {
                let actor: Arc<dyn ensemble_core::actor::Actor> = match spec.kind {
                    SpecKind::User => Arc::new(UserActor::new(
                        spec.id.clone(),
                        spec.model.clone().unwrap_or_else(|| "user-model".into()),
                        backend.clone(),
                    )),
                    SpecKind::Agent => Arc::new(AgentActor::new(
                        spec.id.clone(),
                        spec.model.clone().unwrap_or_else(|| "agent-model".into()),
                        backend.clone(),
                        tools.clone(),
                    )),
                };
                handles.push((spec.id, actor));
            }
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
                if to.as_str() == "__world__" {
                    let payload = match msg {
                        Message::ToolCall { name, args } => EventPayload::ToolCall { name, args },
                        Message::UserMessage { text } => EventPayload::UserMessage { text },
                        Message::AgentMessage { text } => EventPayload::AgentMessage { text },
                        Message::ToolResult { name, result } => EventPayload::ToolResult { name, result },
                        Message::System { note } => EventPayload::System { note },
                    };
                    bus.append_event(Some(from), payload).await;
                } else {
                    bus.send(from, Recipient::Actor(to), msg).await.ok();
                }
            }
            scheduler.run().await
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
            let backend = inner.backend.clone() as Arc<dyn ensemble_runtime::LLMBackend>;
            let tools = inner.tools.clone();
            let bus = inner.bus.clone();
            let mut handles: Vec<(ActorId, Arc<dyn ensemble_core::actor::Actor>)> = Vec::new();
            for spec in inner.actors.drain(..) {
                let actor: Arc<dyn ensemble_core::actor::Actor> = match spec.kind {
                    SpecKind::User => Arc::new(UserActor::new(
                        spec.id.clone(),
                        spec.model.clone().unwrap_or_else(|| "user-model".into()),
                        backend.clone(),
                    )),
                    SpecKind::Agent => Arc::new(AgentActor::new(
                        spec.id.clone(),
                        spec.model.clone().unwrap_or_else(|| "agent-model".into()),
                        backend.clone(),
                        tools.clone(),
                    )),
                };
                handles.push((spec.id, actor));
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
                if to.as_str() == "__world__" {
                    let payload = match msg {
                        Message::ToolCall { name, args } => EventPayload::ToolCall { name, args },
                        Message::UserMessage { text } => EventPayload::UserMessage { text },
                        Message::AgentMessage { text } => EventPayload::AgentMessage { text },
                        Message::ToolResult { name, result } => EventPayload::ToolResult { name, result },
                        Message::System { note } => EventPayload::System { note },
                    };
                    bus.append_event(Some(from), payload).await;
                } else {
                    bus.send(from, Recipient::Actor(to), msg).await.ok();
                }
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
}

#[pymethods]
impl User {
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

    /// Seed an action as a tool call from this user. Logged as a
    /// ToolCall event at run() time so the trace shows the user's
    /// intent. Args is a JSON string; the python wrapper builds it.
    fn act_json(&self, tool: &str, args_json: &str) -> PyResult<()> {
        let args: serde_json::Value = serde_json::from_str(args_json)
            .map_err(|e| PyValueError::new_err(format!("bad json: {e}")))?;
        self.world.lock().seed_messages.push((
            ActorId::from_label(&self.id),
            ActorId::from_label("__world__"),
            Message::ToolCall { name: tool.into(), args },
        ));
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
