use serde::Serialize;
use std::collections::HashMap;
use std::sync::Arc;

use ensemble_core::error::ToolError;

use crate::backend::ToolSchema;

/// A tool the runtime can dispatch on behalf of an agent. Tools are
/// world-agnostic: the closure receives parsed JSON args and produces
/// a JSON effect. World-specific code lives in the closure.
#[derive(Clone)]
pub struct Tool {
    pub schema: ToolSchema,
    pub run: Arc<dyn Fn(&serde_json::Value) -> Result<serde_json::Value, ToolError> + Send + Sync>,
}

impl Tool {
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
                serde_json::to_value(effect)
                    .map_err(|e| ToolError::Execution(format!("serialize: {e}")))
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
    ) -> Result<serde_json::Value, ToolError> {
        let tool = self
            .tools
            .get(name)
            .ok_or_else(|| ToolError::UnknownTool(name.into()))?;
        (tool.run)(args)
    }
}
