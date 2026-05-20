use async_trait::async_trait;
use parking_lot::Mutex;
use std::sync::Arc;

use crate::backend::{
    BackendError, CompletionRequest, CompletionResponse, LLMBackend, ProposedToolCall,
};

/// A single canned turn the mock will produce.
#[derive(Clone, Debug)]
pub struct MockTurn {
    pub text: String,
    pub tool_calls: Vec<ProposedToolCall>,
    pub stop_reason: Option<String>,
}

impl MockTurn {
    pub fn text(s: impl Into<String>) -> Self {
        Self {
            text: s.into(),
            tool_calls: vec![],
            stop_reason: Some("end_turn".into()),
        }
    }

    pub fn tool(name: impl Into<String>, args: serde_json::Value) -> Self {
        Self {
            text: String::new(),
            tool_calls: vec![ProposedToolCall {
                id: None,
                name: name.into(),
                args,
            }],
            stop_reason: Some("tool_use".into()),
        }
    }

    /// A turn that emits text and a tool call in the same step. Mirrors
    /// how real frontier models often say "Let me look that up" right
    /// before issuing the tool call.
    pub fn say_then_tool(
        text: impl Into<String>,
        tool: impl Into<String>,
        args: serde_json::Value,
    ) -> Self {
        Self {
            text: text.into(),
            tool_calls: vec![ProposedToolCall {
                id: None,
                name: tool.into(),
                args,
            }],
            stop_reason: Some("tool_use".into()),
        }
    }
}

/// A scripted sequence of responses, optionally keyed by model name.
/// If the script runs out it returns a final empty response.
#[derive(Default, Clone)]
pub struct MockScript {
    by_model: Arc<Mutex<std::collections::HashMap<String, Vec<MockTurn>>>>,
    default: Arc<Mutex<Vec<MockTurn>>>,
}

impl MockScript {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn push(&self, turn: MockTurn) {
        self.default.lock().push(turn);
    }

    pub fn push_for(&self, model: impl Into<String>, turn: MockTurn) {
        self.by_model.lock().entry(model.into()).or_default().push(turn);
    }

    fn take(&self, model: &str) -> Option<MockTurn> {
        let mut by_model = self.by_model.lock();
        if let Some(q) = by_model.get_mut(model) {
            if !q.is_empty() {
                return Some(q.remove(0));
            }
        }
        drop(by_model);
        let mut default = self.default.lock();
        if default.is_empty() {
            None
        } else {
            Some(default.remove(0))
        }
    }
}

/// A deterministic mock backend used in tests and to bake demo traces.
#[derive(Clone)]
pub struct MockBackend {
    script: MockScript,
}

impl MockBackend {
    pub fn new(script: MockScript) -> Self {
        Self { script }
    }

    pub fn script(&self) -> MockScript {
        self.script.clone()
    }
}

#[async_trait]
impl LLMBackend for MockBackend {
    async fn complete(
        &self,
        request: CompletionRequest,
    ) -> Result<CompletionResponse, BackendError> {
        let turn = self.script.take(&request.model).unwrap_or_else(|| MockTurn {
            text: String::new(),
            tool_calls: vec![],
            stop_reason: Some("script_exhausted".into()),
        });
        Ok(CompletionResponse {
            text: turn.text,
            tool_calls: turn.tool_calls,
            stop_reason: turn.stop_reason,
            usage: None,
            reasoning_text: None,
        })
    }
}
