use serde::Serialize;
use std::collections::HashMap;
use std::sync::Arc;

use ensemble_core::error::ToolError;

use crate::backend::ToolSchema;

/// What a tool produced: a JSON `effect` always, plus an optional
/// `diff` that describes the world-state change. Tools that mutate
/// state should emit a diff so the trace viewer can render a row in
/// its state-change panel; pure lookups leave it `None`.
#[derive(Clone, Debug)]
pub struct ToolOutcome {
    pub effect: serde_json::Value,
    pub diff: Option<serde_json::Value>,
}

impl ToolOutcome {
    pub fn effect_only(effect: serde_json::Value) -> Self {
        Self { effect, diff: None }
    }

    pub fn with_diff(effect: serde_json::Value, diff: serde_json::Value) -> Self {
        Self { effect, diff: Some(diff) }
    }
}

/// A tool the runtime can dispatch on behalf of an agent. Tools are
/// world-agnostic: the closure receives parsed JSON args and produces
/// a `ToolOutcome`. World-specific code lives in the closure.
#[derive(Clone)]
pub struct Tool {
    pub schema: ToolSchema,
    pub run: Arc<dyn Fn(&serde_json::Value) -> Result<ToolOutcome, ToolError> + Send + Sync>,
}

impl Tool {
    /// Lookup-style tool: returns only an effect, no state diff.
    pub fn new<F, E>(
        name: impl Into<String>,
        description: impl Into<String>,
        parameters: serde_json::Value,
        run: F,
    ) -> Self
    where
        F: Fn(&serde_json::Value) -> Result<E, ToolError> + Send + Sync + 'static,
        E: Serialize,
    {
        let name = name.into();
        let description = description.into();
        Self {
            schema: ToolSchema { name, description, parameters },
            run: Arc::new(move |args| {
                let effect = run(args)?;
                let v = serde_json::to_value(effect)
                    .map_err(|e| ToolError::Execution(format!("serialize: {e}")))?;
                Ok(ToolOutcome::effect_only(v))
            }),
        }
    }

    /// State-changing tool: returns both an effect and a diff. The diff
    /// is emitted as a StateDiff event by the runtime after the
    /// ToolResult. Use a diff shape the viewer's state panel expects
    /// (`{table, row_id, field, old, new}` or a list of those).
    pub fn new_with_diff<F, E, D>(
        name: impl Into<String>,
        description: impl Into<String>,
        parameters: serde_json::Value,
        run: F,
    ) -> Self
    where
        F: Fn(&serde_json::Value) -> Result<(E, D), ToolError> + Send + Sync + 'static,
        E: Serialize,
        D: Serialize,
    {
        let name = name.into();
        let description = description.into();
        Self {
            schema: ToolSchema { name, description, parameters },
            run: Arc::new(move |args| {
                let (effect, diff) = run(args)?;
                let ev = serde_json::to_value(effect)
                    .map_err(|e| ToolError::Execution(format!("serialize effect: {e}")))?;
                let dv = serde_json::to_value(diff)
                    .map_err(|e| ToolError::Execution(format!("serialize diff: {e}")))?;
                Ok(ToolOutcome::with_diff(ev, dv))
            }),
        }
    }
}

#[derive(Default, Clone)]
pub struct ToolRegistry {
    tools: HashMap<String, Tool>,
}

impl ToolRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn register(&mut self, tool: Tool) {
        self.tools.insert(tool.schema.name.clone(), tool);
    }

    pub fn get(&self, name: &str) -> Option<&Tool> {
        self.tools.get(name)
    }

    pub fn schemas(&self) -> Vec<ToolSchema> {
        self.tools.values().map(|t| t.schema.clone()).collect()
    }

    pub fn dispatch(
        &self,
        name: &str,
        args: &serde_json::Value,
    ) -> Result<ToolOutcome, ToolError> {
        let tool = self
            .tools
            .get(name)
            .ok_or_else(|| ToolError::UnknownTool(name.into()))?;
        (tool.run)(args)
    }
}
