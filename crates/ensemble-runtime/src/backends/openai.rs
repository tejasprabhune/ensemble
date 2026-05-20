use async_trait::async_trait;
use reqwest::Client;
use serde::Deserialize;

use crate::backend::{
    BackendError, CompletionRequest, CompletionResponse, LLMBackend, ProposedToolCall, Usage,
};
use crate::pricing::{usd_for, Provider};

/// OpenAI Chat Completions client with function calling. No streaming.
///
/// Multi-turn tool use needs ``tool_calls`` on the assistant message
/// and ``tool_call_id`` on the tool reply: omit either and the API
/// returns a 400 the moment the model issues a second tool. This
/// backend serialises whatever the runtime's ``ChatMessage`` carries,
/// so the agent loop's history (which keeps the original call ids)
/// round-trips cleanly.
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
            let mut obj = serde_json::Map::new();
            obj.insert("role".into(), serde_json::Value::String(m.role.clone()));
            // OpenAI rejects an assistant message that has neither
            // content nor tool_calls. When the model issued only tool
            // calls (no text) the content is the empty string, which
            // is still valid.
            obj.insert(
                "content".into(),
                serde_json::Value::String(m.content.clone()),
            );
            if let Some(name) = &m.name {
                obj.insert("name".into(), serde_json::Value::String(name.clone()));
            }
            if !m.tool_calls.is_empty() {
                let calls: Vec<serde_json::Value> = m
                    .tool_calls
                    .iter()
                    .map(|c| {
                        serde_json::json!({
                            "id": c.id.clone().unwrap_or_else(|| "call_unknown".into()),
                            "type": "function",
                            "function": {
                                "name": c.name,
                                "arguments": serde_json::to_string(&c.args).unwrap_or_else(|_| "{}".into()),
                            },
                        })
                    })
                    .collect();
                obj.insert("tool_calls".into(), serde_json::Value::Array(calls));
            }
            if let Some(tcid) = &m.tool_call_id {
                obj.insert(
                    "tool_call_id".into(),
                    serde_json::Value::String(tcid.clone()),
                );
            }
            messages.push(serde_json::Value::Object(obj));
        }
        let mut body = serde_json::json!({
            "model": request.model,
            "messages": messages,
        });
        // Reasoning models (gpt-5, o1, o3) and many Azure deployments
        // reject explicit temperature or max_completion_tokens; only
        // emit them when the scenario opted in.
        if let Some(temp) = request.temperature {
            body["temperature"] = serde_json::json!(temp);
        }
        if let Some(max_tokens) = request.max_tokens {
            body["max_completion_tokens"] = serde_json::json!(max_tokens);
        }
        // Forward open-ended params verbatim. Keys the API does not
        // recognise are surfaced by the API itself.
        if let serde_json::Value::Object(map) = &mut body {
            for (k, v) in &request.extra_params {
                map.insert(k.clone(), v.clone());
            }
        }
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
