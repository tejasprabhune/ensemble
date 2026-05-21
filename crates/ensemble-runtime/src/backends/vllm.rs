use async_trait::async_trait;
use reqwest::Client;
use serde::Deserialize;

use crate::backend::{
    BackendError, CompletionRequest, CompletionResponse, LLMBackend, ProposedToolCall, Usage,
};
use crate::pricing::{usd_for, Provider};

/// Hits a vLLM-compatible OpenAI-shaped HTTP server. Use this for
/// serving trained persona adapters. `adapter` is optional and is
/// forwarded as the `model` field if set, allowing one base server to
/// serve many adapters.
pub struct LocalAdapterBackend {
    base_url: String,
    adapter: Option<String>,
    client: Client,
}

impl LocalAdapterBackend {
    pub fn new(base_url: impl Into<String>) -> Self {
        Self {
            base_url: base_url.into(),
            adapter: None,
            client: Client::new(),
        }
    }

    pub fn with_adapter(mut self, adapter: impl Into<String>) -> Self {
        self.adapter = Some(adapter.into());
        self
    }
}

#[derive(Deserialize)]
struct ChatResponse {
    choices: Vec<Choice>,
    #[serde(default)]
    usage: Option<VllmUsage>,
}

#[derive(Deserialize)]
struct VllmUsage {
    #[serde(default)]
    prompt_tokens: u64,
    #[serde(default)]
    completion_tokens: u64,
}

#[derive(Deserialize)]
struct Choice {
    message: ChoiceMessage,
    finish_reason: Option<String>,
}

#[derive(Deserialize)]
struct ChoiceMessage {
    #[serde(default)]
    content: Option<String>,
    #[serde(default)]
    tool_calls: Vec<ToolCallBlock>,
}

#[derive(Deserialize)]
struct ToolCallBlock {
    #[serde(default)]
    id: Option<String>,
    function: FunctionCall,
}

#[derive(Deserialize)]
struct FunctionCall {
    name: String,
    arguments: String,
}

#[async_trait]
impl LLMBackend for LocalAdapterBackend {
    async fn complete(
        &self,
        request: CompletionRequest,
    ) -> Result<CompletionResponse, BackendError> {
        let model = self
            .adapter
            .clone()
            .unwrap_or_else(|| request.model.clone());
        let mut messages: Vec<serde_json::Value> = Vec::new();
        if let Some(sys) = &request.system {
            messages.push(serde_json::json!({ "role": "system", "content": sys }));
        }
        for m in &request.messages {
            messages.push(serde_json::json!({ "role": m.role, "content": m.content }));
        }
        let mut body = serde_json::json!({
            "model": model,
            "messages": messages,
            "max_tokens": request.max_tokens.unwrap_or(1024),
            "temperature": request.temperature.unwrap_or(0.7),
        });
        if !request.tools.is_empty() {
            let tools: Vec<serde_json::Value> = request
                .tools
                .iter()
                .map(|t| {
                    serde_json::json!({
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.parameters,
                        }
                    })
                })
                .collect();
            body["tools"] = serde_json::Value::Array(tools);
        }

        let resp = self
            .client
            .post(format!(
                "{}/chat/completions",
                self.base_url.trim_end_matches('/')
            ))
            .json(&body)
            .send()
            .await
            .map_err(|e| BackendError::Transport(e.to_string()))?;

        if !resp.status().is_success() {
            let status = resp.status();
            let text = resp.text().await.unwrap_or_default();
            return Err(BackendError::Rejected(format!("{status}: {text}")));
        }

        let parsed: ChatResponse = resp
            .json()
            .await
            .map_err(|e| BackendError::Malformed(e.to_string()))?;

        let usage = parsed.usage.as_ref().map(|u| {
            let usd = usd_for(
                Provider::OpenAI,
                &model,
                u.prompt_tokens,
                u.completion_tokens,
            );
            Usage {
                input_tokens: u.prompt_tokens,
                output_tokens: u.completion_tokens,
                usd,
            }
        });
        let Some(choice) = parsed.choices.into_iter().next() else {
            return Err(BackendError::Malformed("no choices returned".into()));
        };
        let text = choice.message.content.unwrap_or_default();
        let tool_calls = choice
            .message
            .tool_calls
            .into_iter()
            .map(|tc| {
                let args = serde_json::from_str(&tc.function.arguments)
                    .unwrap_or_else(|_| serde_json::Value::Null);
                ProposedToolCall {
                    id: tc.id,
                    name: tc.function.name,
                    args,
                }
            })
            .collect();
        Ok(CompletionResponse {
            text,
            tool_calls,
            stop_reason: choice.finish_reason,
            usage,
            reasoning_text: None,
        })
    }
}
