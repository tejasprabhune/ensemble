use async_trait::async_trait;
use reqwest::Client;
use serde::Deserialize;

use crate::backend::{
    BackendError, CompletionRequest, CompletionResponse, LLMBackend, ProposedToolCall, Usage,
};
use crate::pricing::{usd_for, Provider};

/// Minimal Anthropic Messages API client. Tool use is supported via the
/// official `tools` and `tool_use` content blocks. Streaming is off; v0
/// is non-streaming by design.
pub struct AnthropicBackend {
    api_key: String,
    base_url: String,
    client: Client,
    anthropic_version: String,
}

impl AnthropicBackend {
    pub fn from_env() -> Result<Self, BackendError> {
        let api_key = std::env::var("ANTHROPIC_API_KEY")
            .map_err(|_| BackendError::Rejected("ANTHROPIC_API_KEY not set".into()))?;
        Ok(Self::with_key(api_key))
    }

    pub fn with_key(api_key: impl Into<String>) -> Self {
        Self {
            api_key: api_key.into(),
            base_url: "https://api.anthropic.com/v1".into(),
            client: Client::new(),
            anthropic_version: "2023-06-01".into(),
        }
    }

    pub fn with_base_url(mut self, base_url: impl Into<String>) -> Self {
        self.base_url = base_url.into();
        self
    }
}

#[derive(Deserialize)]
struct AnthropicResponse {
    content: Vec<AnthropicBlock>,
    stop_reason: Option<String>,
    #[serde(default)]
    usage: Option<AnthropicUsage>,
}

#[derive(Deserialize)]
struct AnthropicUsage {
    #[serde(default)]
    input_tokens: u64,
    #[serde(default)]
    output_tokens: u64,
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum AnthropicBlock {
    Text { text: String },
    ToolUse { id: String, name: String, input: serde_json::Value },
}

#[async_trait]
impl LLMBackend for AnthropicBackend {
    async fn complete(
        &self,
        request: CompletionRequest,
    ) -> Result<CompletionResponse, BackendError> {
        let mut body = serde_json::json!({
            "model": request.model,
            "system": request.system,
            "messages": request.messages,
            "tools": request.tools.iter().map(|t| serde_json::json!({
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
            })).collect::<Vec<_>>(),
            "max_tokens": request.max_tokens.unwrap_or(4096),
        });
        if let Some(temp) = request.temperature {
            body["temperature"] = serde_json::json!(temp);
        }
        if let serde_json::Value::Object(map) = &mut body {
            for (k, v) in &request.extra_params {
                map.insert(k.clone(), v.clone());
            }
        }

        let resp = self
            .client
            .post(format!("{}/messages", self.base_url))
            .header("x-api-key", &self.api_key)
            .header("anthropic-version", &self.anthropic_version)
            .json(&body)
            .send()
            .await
            .map_err(|e| BackendError::Transport(e.to_string()))?;

        if !resp.status().is_success() {
            let status = resp.status();
            let text = resp.text().await.unwrap_or_default();
            return Err(BackendError::Rejected(format!("{status}: {text}")));
        }

        let parsed: AnthropicResponse = resp
            .json()
            .await
            .map_err(|e| BackendError::Malformed(e.to_string()))?;

        let mut text = String::new();
        let mut tool_calls = Vec::new();
        for block in parsed.content {
            match block {
                AnthropicBlock::Text { text: t } => {
                    if !text.is_empty() {
                        text.push('\n');
                    }
                    text.push_str(&t);
                }
                AnthropicBlock::ToolUse { id, name, input } => {
                    tool_calls.push(ProposedToolCall {
                        id: Some(id),
                        name,
                        args: input,
                    });
                }
            }
        }

        let usage = parsed.usage.map(|u| {
            let usd = usd_for(
                Provider::Anthropic,
                &request.model,
                u.input_tokens,
                u.output_tokens,
            );
            Usage {
                input_tokens: u.input_tokens,
                output_tokens: u.output_tokens,
                usd,
            }
        });

        Ok(CompletionResponse {
            text,
            tool_calls,
            stop_reason: parsed.stop_reason,
            usage,
        })
    }
}
