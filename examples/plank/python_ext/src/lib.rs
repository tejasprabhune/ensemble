//! Plank's python extension.
//!
//! Exposes a `PlankDb` pyclass that wraps the Rust `PlankState` (a
//! seeded SQLite database) and a single `dispatch` method routing tool
//! names to plank's tool registry. Each `PlankDb` instance has its
//! own state, so a python ensemble world that calls `PlankDb()` from
//! a per-World setup factory gets isolated state per scenario run.

use std::sync::Arc;

use parking_lot::Mutex;
use pyo3::exceptions::{PyKeyError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;

use plank::state::PlankState;
use plank::{predicates, register_all};

use ensemble_core::predicate::{PredicateCtx, PredicateRegistry};
use ensemble_runtime::ToolRegistry;

#[pyclass]
struct PlankDb {
    tools: ToolRegistry,
    predicates: PredicateRegistry,
}

#[pymethods]
impl PlankDb {
    #[new]
    fn new() -> Self {
        let state = Arc::new(Mutex::new(PlankState::seed_default()));
        let tools = ToolRegistry::new();
        register_all(&state, &tools);
        let preds = PredicateRegistry::new();
        predicates::register_all(&state, &preds);
        Self { tools, predicates: preds }
    }

    /// Names of the tools this PlankDb instance carries.
    fn tool_names(&self) -> Vec<String> {
        self.tools.names()
    }

    /// Names of the predicates this PlankDb instance carries.
    fn predicate_names(&self) -> Vec<String> {
        self.predicates.names()
    }

    /// Dispatch a tool by name. `args_json` is a JSON string; the
    /// return is a JSON string of `{"effect": ..., "diff"?: ...}` so
    /// the wrapper plays cleanly with ensemble's plugin tool ABI.
    fn dispatch(&self, name: &str, args_json: &str) -> PyResult<String> {
        let args: serde_json::Value = serde_json::from_str(args_json)
            .map_err(|e| PyValueError::new_err(format!("bad args json: {e}")))?;
        let outcome = self
            .tools
            .dispatch(name, &args)
            .map_err(|e| PyRuntimeError::new_err(format!("{e}")))?;
        let mut out = serde_json::Map::new();
        out.insert("effect".into(), outcome.effect);
        if let Some(diff) = outcome.diff {
            out.insert("diff".into(), diff);
        }
        Ok(serde_json::Value::Object(out).to_string())
    }

    /// Evaluate a predicate by name against the supplied trace JSON
    /// and args JSON. Returns false (not an exception) for unknown
    /// predicates so graders stay robust to plug-in worlds.
    fn evaluate_predicate(
        &self,
        name: &str,
        trace_json: &str,
        args_json: &str,
    ) -> PyResult<bool> {
        let trace: Vec<ensemble_core::event::Event> =
            serde_json::from_str(trace_json).map_err(|e| {
                PyValueError::new_err(format!("bad trace json: {e}"))
            })?;
        let args: serde_json::Value = if args_json.is_empty() {
            serde_json::Value::Null
        } else {
            serde_json::from_str(args_json)
                .map_err(|e| PyValueError::new_err(format!("bad args json: {e}")))?
        };
        let ctx = PredicateCtx::with_args(&trace, args);
        self.predicates
            .evaluate(name, &ctx)
            .ok_or_else(|| PyKeyError::new_err(format!("no predicate {name:?}")))
    }
}

#[pymodule]
fn _native(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PlankDb>()?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
