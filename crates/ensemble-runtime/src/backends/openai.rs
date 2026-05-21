use async_trait::async_trait;
use reqwest::Client;
use serde::Deserialize;

use crate::backend::{
    BackendError, CompletionRequest, CompletionResponse, LLMBackend, ProposedToolCall, Usage,
};
use crate::pricing::{usd_for, Provider};

/// OpenAI Responses API client.
///
/// The Responses API supersedes Chat Completions for the modern model
/// fleet (gpt-5, o-series): it exposes reasoning summaries on output,
/// uses a typed item list instead of free-form messages, and is the
/// API the OpenAI SDK now writes against by default. The Chat
/// Completions path is preserved as an opt-in fallback for
/// deployments that have not finished the migration; set
/// ``ENSEMBLE_OPENAI_API=chat`` to switch.
///
/// Multi-turn tool use threads through ``function_call`` /
/// ``function_call_output`` items: the runtime's ``ChatMessage``
/// history carries ``tool_calls`` on assistant messages and
/// ``tool_call_id`` on tool replies, and this backend translates them
/// into the typed item list on every call. Stateless: each request
/// resends the full history. Reasoning continuity between turns is
/// not preserved (that requires the stateful path with
/// ``previous_response_id``); the model reasons fresh per turn but
/// the *summary* of each turn's reasoning is exposed via
/// ``CompletionResponse.reasoning_text`` so the runtime can record it
/// on the trace.
pub struct OpenAIBackend {
    api_key: String,
    base_url: String,
    client: Client,
    api_kind: ApiKind,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ApiKind {
    Responses,
    Chat,
}

fn resolve_api_kind() -> ApiKind {
    match std::env::var("ENSEMBLE_OPENAI_API").as_deref() {
        Ok("chat") | Ok("chat_completions") => ApiKind::Chat,
        _ => ApiKind::Responses,
    }
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
            api_kind: resolve_api_kind(),
        }
    }

    pub fn with_base_url(mut self, base_url: impl Into<String>) -> Self {
        self.base_url = base_url.into();
        self
    }
}

#[async_trait]
impl LLMBackend for OpenAIBackend {
    async fn complete(
        &self,
        request: CompletionRequest,
    ) -> Result<CompletionResponse, BackendError> {
        match self.api_kind {
            ApiKind::Responses => self.complete_responses(request).await,
            ApiKind::Chat => self.complete_chat(request).await,
        }
    }
}

#[derive(Deserialize)]
struct ResponsesResponse {
    #[serde(default)]
    output: Vec<ResponseOutputItem>,
    #[serde(default)]
    usage: Option<ResponsesUsage>,
    #[serde(default)]
    status: Option<String>,
}

#[derive(Deserialize)]
#[serde(tag = "type")]
enum ResponseOutputItem {
    #[serde(rename = "reasoning")]
    Reasoning {
        #[serde(default)]
        summary: Vec<ReasoningSummary>,
    },
    #[serde(rename = "message")]
    Message {
        #[serde(default)]
        content: Vec<MessageContent>,
    },
    #[serde(rename = "function_call")]
    FunctionCall {
        #[serde(default)]
        call_id: Option<String>,
        #[serde(default)]
        id: Option<String>,
        name: String,
        arguments: String,
    },
    #[serde(other)]
    Other,
}

#[derive(Deserialize)]
struct ReasoningSummary {
    #[serde(default)]
    text: String,
}

#[derive(Deserialize)]
#[serde(tag = "type")]
enum MessageContent {
    #[serde(rename = "output_text")]
    OutputText {
        #[serde(default)]
        text: String,
    },
    #[serde(other)]
    Other,
}

#[derive(Deserialize, Default)]
struct ResponsesUsage {
    #[serde(default)]
    input_tokens: u64,
    #[serde(default)]
    output_tokens: u64,
}

impl OpenAIBackend {
    async fn complete_responses(
        &self,
        request: CompletionRequest,
    ) -> Result<CompletionResponse, BackendError> {
        // Build the input item list. system → instructions; user /
        // assistant → role-typed input items; the assistant's
        // tool_calls split into function_call items; tool replies
        // become function_call_output items keyed by tool_call_id.
        let mut input: Vec<serde_json::Value> = Vec::new();
        for m in &request.messages {
            match m.role.as_str() {
                "system" => {
                    // Captured once into `instructions` below.
                }
                "user" => {
                    let mut obj = serde_json::Map::new();
                    obj.insert("role".into(), serde_json::Value::String("user".into()));
                    obj.insert(
                        "content".into(),
                        serde_json::Value::String(m.content.clone()),
                    );
                    if let Some(name) = &m.name {
                        obj.insert("name".into(), serde_json::Value::String(name.clone()));
                    }
                    input.push(serde_json::Value::Object(obj));
                }
                "assistant" => {
                    if !m.content.is_empty() {
                        input.push(serde_json::json!({
                            "role": "assistant",
                            "content": m.content,
                        }));
                    }
                    for call in &m.tool_calls {
                        let call_id = call.id.clone().unwrap_or_else(|| "call_unknown".into());
                        let args =
                            serde_json::to_string(&call.args).unwrap_or_else(|_| "{}".into());
                        input.push(serde_json::json!({
                            "type": "function_call",
                            "call_id": call_id,
                            "name": call.name,
                            "arguments": args,
                        }));
                    }
                }
                "tool" => {
                    let call_id = m
                        .tool_call_id
                        .clone()
                        .unwrap_or_else(|| "call_unknown".into());
                    input.push(serde_json::json!({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": m.content,
                    }));
                }
                _ => { /* skip unknown roles */ }
            }
        }
        let instructions = request.system.clone().or_else(|| {
            request
                .messages
                .iter()
                .find(|m| m.role == "system")
                .map(|m| m.content.clone())
        });

        let mut body = serde_json::json!({
            "model": request.model,
            "input": input,
            "store": false,
        });
        if let Some(inst) = instructions {
            body["instructions"] = serde_json::Value::String(inst);
        }
        if let Some(temp) = request.temperature {
            body["temperature"] = serde_json::json!(temp);
        }
        if let Some(max_tokens) = request.max_tokens {
            body["max_output_tokens"] = serde_json::json!(max_tokens);
        }
        if !request.tools.is_empty() {
            // Responses API tool schema is flatter than Chat
            // Completions': type=function with name/description/
            // parameters at the same level (no wrapping function
            // sub-object).
            let tools: Vec<serde_json::Value> = request
                .tools
                .iter()
                .map(|t| {
                    serde_json::json!({
                        "type": "function",
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    })
                })
                .collect();
            body["tools"] = serde_json::Value::Array(tools);
        }
        if let serde_json::Value::Object(map) = &mut body {
            // Migrate the Chat Completions flat `reasoning_effort`
            // field to the Responses API's nested `reasoning.effort`
            // shape, and request a summary so the reasoning text
            // lands in the trace. Callers that pass `reasoning`
            // directly stay in control.
            for (k, v) in &request.extra_params {
                if k == "reasoning_effort" {
                    let existing = map
                        .get("reasoning")
                        .cloned()
                        .unwrap_or_else(|| serde_json::json!({}));
                    if let serde_json::Value::Object(mut rmap) = existing {
                        rmap.insert("effort".to_string(), v.clone());
                        rmap.entry("summary".to_string())
                            .or_insert(serde_json::Value::String("auto".into()));
                        map.insert("reasoning".to_string(), serde_json::Value::Object(rmap));
                    }
                } else {
                    map.insert(k.clone(), v.clone());
                }
            }
            // Default `reasoning: {summary: "auto"}` so the API
            // returns a reasoning summary on every call. The model
            // surfaces it through CompletionResponse.reasoning_text
            // and the agent loop emits it on the bus as an
            // agent_message before the tool_calls. Reasoning-capable
            // models honor the request; non-reasoning models ignore
            // the field. Callers that want to suppress reasoning can
            // pass `params={"reasoning": {"summary": "none"}}`.
            if !map.contains_key("reasoning") {
                map.insert(
                    "reasoning".to_string(),
                    serde_json::json!({"summary": "auto"}),
                );
            } else if let Some(serde_json::Value::Object(rmap)) = map.get_mut("reasoning") {
                rmap.entry("summary".to_string())
                    .or_insert(serde_json::Value::String("auto".into()));
            }
        }

        let resp = self
            .client
            .post(format!("{}/responses", self.base_url))
            .bearer_auth(&self.api_key)
            .json(&body)
            .send()
            .await
            .map_err(|e| BackendError::Transport(e.to_string()))?;

        if !resp.status().is_success() {
            let status = resp.status();
            let text = resp.text().await.unwrap_or_default();
            return Err(BackendError::Rejected(super::auth_hint::format_rejection(
                super::auth_hint::Provider::OpenAI,
                status,
                &text,
            )));
        }

        let parsed: ResponsesResponse = resp
            .json()
            .await
            .map_err(|e| BackendError::Malformed(e.to_string()))?;

        let usage = parsed.usage.as_ref().map(|u| {
            let usd = usd_for(
                Provider::OpenAI,
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

        // Fold the output items into text + reasoning summary +
        // tool calls. The viewer renders reasoning_text just like
        // agent_message; the runtime emits it on the bus before the
        // tool_calls so it appears in chronological order.
        let mut text_parts: Vec<String> = Vec::new();
        let mut reasoning_parts: Vec<String> = Vec::new();
        let mut tool_calls: Vec<ProposedToolCall> = Vec::new();
        for item in parsed.output {
            match item {
                ResponseOutputItem::Reasoning { summary } => {
                    for s in summary {
                        if !s.text.is_empty() {
                            reasoning_parts.push(s.text);
                        }
                    }
                }
                ResponseOutputItem::Message { content } => {
                    for c in content {
                        if let MessageContent::OutputText { text } = c {
                            if !text.is_empty() {
                                text_parts.push(text);
                            }
                        }
                    }
                }
                ResponseOutputItem::FunctionCall {
                    call_id,
                    id,
                    name,
                    arguments,
                } => {
                    let args = serde_json::from_str(&arguments)
                        .unwrap_or_else(|_| serde_json::Value::Null);
                    tool_calls.push(ProposedToolCall {
                        id: call_id.or(id),
                        name,
                        args,
                    });
                }
                ResponseOutputItem::Other => {}
            }
        }
        let text = text_parts.join("\n");
        let reasoning_text = if reasoning_parts.is_empty() {
            None
        } else {
            Some(reasoning_parts.join("\n\n"))
        };

        Ok(CompletionResponse {
            text,
            tool_calls,
            stop_reason: parsed.status,
            usage,
            reasoning_text,
        })
    }

    async fn complete_chat(
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
        if let Some(temp) = request.temperature {
            body["temperature"] = serde_json::json!(temp);
        }
        if let Some(max_tokens) = request.max_tokens {
            body["max_completion_tokens"] = serde_json::json!(max_tokens);
        }
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
            return Err(BackendError::Rejected(super::auth_hint::format_rejection(
                super::auth_hint::Provider::OpenAI,
                status,
                &text,
            )));
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
            reasoning_text: None,
        })
    }
}

/// Chat Completions response types kept for the `ENSEMBLE_OPENAI_API=chat`
/// opt-in fallback.
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
