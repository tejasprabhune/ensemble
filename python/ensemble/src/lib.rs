use std::sync::Arc;

use parking_lot::Mutex;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyList;

use ensemble_core::bus::{Bus, Message};
use ensemble_core::event::EventLog;
use ensemble_core::ids::ActorId;
use ensemble_core::scheduler::TickBudget;
use ensemble_runtime::{MockBackend, MockScript, MockTurn, ToolRegistry};

mod world_registry;
use world_registry::WorldRegistry;

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
        if !WorldRegistry::contains(&name) {
            return Err(PyValueError::new_err(format!(
                "no world named {name:?}; register one before constructing it"
            )));
        }
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
                tools: Arc::new(ToolRegistry::new()),
                actors: vec![],
                seed_messages: vec![],
                budget: TickBudget::default(),
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

#[pymodule]
fn _native(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Always-available no-op world for tests and the scaffold flow.
    WorldRegistry::register("noop");

    m.add_class::<World>()?;
    m.add_class::<User>()?;
    m.add_class::<Agent>()?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
