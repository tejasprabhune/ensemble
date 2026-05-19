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

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CompletionRequest {
    pub model: String,
    pub system: Option<String>,
    pub messages: Vec<ChatMessage>,
    pub tools: Vec<ToolSchema>,
    pub temperature: Option<f32>,
    pub max_tokens: Option<u32>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CompletionResponse {
    pub text: String,
    pub tool_calls: Vec<ProposedToolCall>,
    pub stop_reason: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ProposedToolCall {
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
