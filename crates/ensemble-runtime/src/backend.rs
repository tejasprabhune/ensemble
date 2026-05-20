use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use thiserror::Error;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ToolSchema {
    pub name: String,
    pub description: String,
    pub parameters: serde_json::Value,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ChatMessage {
    pub role: String,
    pub content: String,
}

impl ChatMessage {
    pub fn user(content: impl Into<String>) -> Self {
        Self { role: "user".into(), content: content.into() }
    }
    pub fn assistant(content: impl Into<String>) -> Self {
        Self { role: "assistant".into(), content: content.into() }
    }
    pub fn system(content: impl Into<String>) -> Self {
        Self { role: "system".into(), content: content.into() }
    }
    pub fn tool(content: impl Into<String>) -> Self {
        Self { role: "tool".into(), content: content.into() }
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

/// Per-completion usage. Backends populate this from the provider's
/// response so the runtime can attribute cost to the actor that
/// issued the call. `usd` is filled in when the model is in
/// `crates/ensemble-runtime/pricing.toml`; otherwise it stays `None`
/// and the runtime records token totals only.
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
