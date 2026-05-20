use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::Arc;
use thiserror::Error;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ToolSchema {
    pub name: String,
    pub description: String,
    pub parameters: serde_json::Value,
}

/// A single message in a backend conversation. The shape covers all
/// four role flavors (system, user, assistant, tool) plus the two
/// fields OpenAI's Chat Completions API requires for real multi-turn
/// tool use: ``tool_calls`` on assistant messages and ``tool_call_id``
/// on tool replies. Anthropic does not need these (it sends typed
/// content blocks), but the runtime carries them through every
/// backend so the message log is lossless and any backend can render
/// them as needed.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct ChatMessage {
    pub role: String,
    pub content: String,
    /// Optional speaker label. OpenAI's Chat Completions API forwards
    /// this on user/tool messages so the model can distinguish "the
    /// reviewer agent" from "the simulated user" in a multi-actor
    /// scenario. Anthropic ignores it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    /// Tool calls the assistant emitted on this turn. OpenAI requires
    /// these to live on the assistant message that proposed them; the
    /// matching ``role: "tool"`` reply then carries the
    /// ``tool_call_id``.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub tool_calls: Vec<ProposedToolCall>,
    /// Set on ``role: "tool"`` messages to pair them with the
    /// assistant's earlier ``tool_calls`` entry. Required by OpenAI;
    /// ignored by Anthropic.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
}

impl ChatMessage {
    pub fn user(content: impl Into<String>) -> Self {
        Self { role: "user".into(), content: content.into(), ..Default::default() }
    }
    pub fn assistant(content: impl Into<String>) -> Self {
        Self { role: "assistant".into(), content: content.into(), ..Default::default() }
    }
    pub fn assistant_with_calls(
        content: impl Into<String>,
        tool_calls: Vec<ProposedToolCall>,
    ) -> Self {
        Self {
            role: "assistant".into(),
            content: content.into(),
            tool_calls,
            ..Default::default()
        }
    }
    pub fn system(content: impl Into<String>) -> Self {
        Self { role: "system".into(), content: content.into(), ..Default::default() }
    }
    pub fn tool(content: impl Into<String>) -> Self {
        Self { role: "tool".into(), content: content.into(), ..Default::default() }
    }
    pub fn tool_result(
        tool_call_id: impl Into<String>,
        content: impl Into<String>,
    ) -> Self {
        Self {
            role: "tool".into(),
            content: content.into(),
            tool_call_id: Some(tool_call_id.into()),
            ..Default::default()
        }
    }

    pub fn with_name(mut self, name: impl Into<String>) -> Self {
        self.name = Some(name.into());
        self
    }
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct CompletionRequest {
    pub model: String,
    pub system: Option<String>,
    pub messages: Vec<ChatMessage>,
    pub tools: Vec<ToolSchema>,
    pub temperature: Option<f32>,
    pub max_tokens: Option<u32>,
    /// Open-ended per-actor knobs. Forwarded into the backend's JSON
    /// request as additional top-level fields, so a scenario can pass
    /// ``{"reasoning_effort": "high"}`` or ``{"top_p": 0.95}`` without
    /// the runtime needing a typed field per knob. Backends ignore
    /// keys they do not understand; the underlying API rejects bad
    /// values with its own error.
    #[serde(default, skip_serializing_if = "HashMap::is_empty")]
    pub extra_params: HashMap<String, serde_json::Value>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct CompletionResponse {
    pub text: String,
    pub tool_calls: Vec<ProposedToolCall>,
    pub stop_reason: Option<String>,
    /// Token (and optionally USD) usage for the completion call. Each
    /// shipped backend parses the provider's usage block when one is
    /// present; the runtime records `tokens_in`, `tokens_out`, and
    /// `usd` (when a pricing entry resolves) as cost annotations
    /// against the calling actor.
    #[serde(default)]
    pub usage: Option<Usage>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Usage {
    #[serde(default)]
    pub input_tokens: u64,
    #[serde(default)]
    pub output_tokens: u64,
    #[serde(default)]
    pub usd: Option<f64>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ProposedToolCall {
    /// Provider-supplied call id (Anthropic `tool_use.id`, OpenAI
    /// `tool_calls[].id`). `None` means the runtime should mint one.
    #[serde(default)]
    pub id: Option<String>,
    pub name: String,
    pub args: serde_json::Value,
}

#[derive(Debug, Error)]
pub enum BackendError {
    #[error("backend transport error: {0}")]
    Transport(String),
    #[error("backend returned malformed response: {0}")]
    Malformed(String),
    #[error("backend rejected request: {0}")]
    Rejected(String),
}

#[async_trait]
pub trait LLMBackend: Send + Sync {
    async fn complete(
        &self,
        request: CompletionRequest,
    ) -> Result<CompletionResponse, BackendError>;
}

pub type SharedBackend = Arc<dyn LLMBackend>;
