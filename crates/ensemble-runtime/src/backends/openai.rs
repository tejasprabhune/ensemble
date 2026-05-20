use async_trait::async_trait;
use reqwest::Client;
use serde::Deserialize;

use crate::backend::{
    BackendError, CompletionRequest, CompletionResponse, LLMBackend, ProposedToolCall, Usage,
};
use crate::pricing::{usd_for, Provider};

/// OpenAI Chat Completions client with function calling. No streaming.
pub struct OpenAIBackend {
    api_key: String,
    base_url: String,
    client: Client,
}

impl OpenAIBackend {
    pub fn from_env() -> Result<Self, BackendError> {
        let api_key = std::env::var("OPENAI_API_KEY")
            .map_err(|_| BackendError::Rejected("OPENAI_API_KEY not set".into()))?;
        Ok(Self::with_key(api_key))
    }

    pub fn with_key(api_key: impl Into<String>) -> Self {
        Self {
            api_key: api_key.into(),
            base_url: "https://api.openai.com/v1".into(),
            client: Client::new(),
        }
    }

    pub fn with_base_url(mut self, base_url: impl Into<String>) -> Self {
        self.base_url = base_url.into();
        self
    }
}

#[derive(Deserialize)]
struct ChatResponse {
    choices: Vec<Choice>,
    #[serde(default)]
    usage: Option<OpenAIUsage>,
}

#[derive(Deserialize)]
struct OpenAIUsage {
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
impl LLMBackend for OpenAIBackend {
    async fn complete(
        &self,
        request: CompletionRequest,
    ) -> Result<CompletionResponse, BackendError> {
        let mut messages: Vec<serde_json::Value> = Vec::new();
        if let Some(sys) = &request.system {
            messages.push(serde_json::json!({ "role": "system", "content": sys }));
        }
        for m in &request.messages {
            messages.push(serde_json::json!({ "role": m.role, "content": m.content }));
        }
        let mut body = serde_json::json!({
            "model": request.model,
            "messages": messages,
            // Newer OpenAI / Azure models (gpt-5, o1, ...) reject the
            // legacy `max_tokens` field and require `max_completion_tokens`.
            // The new field has been accepted by every chat-completions
            // model since mid-2024, so we always emit it.
            "max_completion_tokens": request.max_tokens.unwrap_or(1024),
            "temperature": request.temperature.unwrap_or(0.7),
        });
        // Some OpenAI models reject an empty tools array; only attach
        // the field when the agent actually has tools to offer.
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
            .post(format!("{}/chat/completions", self.base_url))
            .bearer_auth(&self.api_key)
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
                &request.model,
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
        })
    }
}
