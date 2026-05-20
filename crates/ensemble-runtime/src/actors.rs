use async_trait::async_trait;
use parking_lot::Mutex;
use std::collections::HashSet;
use std::sync::Arc;

use ensemble_core::actor::{Actor, ActorKind};
use ensemble_core::bus::{Bus, Envelope, Message, Recipient};
use ensemble_core::error::CoreError;
use ensemble_core::event::EventPayload;

use ensemble_core::ids::ActorId;
use ensemble_core::ids::MessageId;

use crate::backend::{ChatMessage, CompletionRequest, CompletionResponse, SharedBackend};
use crate::tools::ToolRegistry;

/// Forward the usage block from a completion response into the bus's
/// cost ledger so the trace attributes tokens (and USD, when the
/// model is in the pricing table) to the actor that issued the
/// call. Backends that omit usage (the mock backend, or a real
/// backend whose API response lacks the block) record nothing.
async fn record_completion_cost(
    bus: &Bus,
    actor: &ActorId,
    response: &CompletionResponse,
) {
    let Some(usage) = response.usage.as_ref() else {
        return;
    };
    if usage.input_tokens > 0 {
        bus.record_cost("tokens_in", usage.input_tokens as f64, Some(actor.clone()))
            .await;
    }
    if usage.output_tokens > 0 {
        bus.record_cost("tokens_out", usage.output_tokens as f64, Some(actor.clone()))
            .await;
    }
    if let Some(usd) = usage.usd {
        bus.record_cost("usd", usd, Some(actor.clone())).await;
    }
}

/// A simulated end-user driven by an LLM. Receives agent messages,
/// asks the backend for a reply, and posts that reply on the bus
/// (broadcast so any agent in the same room can hear it).
pub struct UserActor {
    pub id: ActorId,
    pub model: String,
    pub backend: SharedBackend,
    pub system_prompt: Option<String>,
    history: Mutex<Vec<ChatMessage>>,
}

impl UserActor {
    pub fn new(id: ActorId, model: impl Into<String>, backend: SharedBackend) -> Self {
        Self {
            id,
            model: model.into(),
            backend,
            system_prompt: None,
            history: Mutex::new(Vec::new()),
        }
    }

    pub fn with_system_prompt(mut self, prompt: impl Into<String>) -> Self {
        self.system_prompt = Some(prompt.into());
        self
    }
}

#[async_trait]
impl Actor for UserActor {
    fn id(&self) -> ActorId { self.id.clone() }
    fn kind(&self) -> ActorKind { ActorKind::User }

    async fn step(&self, bus: &Bus, envelope: Envelope) -> Result<(), CoreError> {
        let from = envelope.from.clone();
        let incoming = match envelope.message {
            Message::AgentMessage { text } => text,
            Message::UserMessage { text } => text,
            Message::System { .. } | Message::ToolCall { .. } | Message::ToolResult { .. } => {
                return Ok(());
            }
        };
        // Users hear themselves talked at by the seed scaffold (act_json
        // posts as the user). If an agent never replied, the user's own
        // line ends up routed back; ignore those self-echoes.
        if from == self.id {
            return Ok(());
        }
        {
            let mut h = self.history.lock();
            h.push(ChatMessage::user(incoming));
        }
        let messages = self.history.lock().clone();
        let req = CompletionRequest {
            model: self.model.clone(),
            system: self.system_prompt.clone(),
            messages,
            ..Default::default()
        };
        let resp = match self.backend.complete(req).await {
            Ok(r) => r,
            Err(e) => {
                bus.append_event(
                    Some(self.id.clone()),
                    EventPayload::System {
                        note: format!("backend error ({}): {e}", self.model),
                    },
                )
                .await;
                return Ok(());
            }
        };
        record_completion_cost(bus, &self.id, &resp).await;
        self.history.lock().push(ChatMessage::assistant(resp.text.clone()));
        if !resp.text.is_empty() {
            bus.send(
                self.id.clone(),
                Recipient::Actor(from),
                Message::UserMessage { text: resp.text },
            )
            .await?;
        }
        Ok(())
    }
}

/// An LLM-driven agent that can issue tool calls. Tool calls are
/// dispatched through the registry; the resulting JSON effect is fed
/// back to the model on the next turn. Dispatches go through the
/// async path so the tool's declared timeout and resource locks
/// apply.
pub struct AgentActor {
    pub id: ActorId,
    pub model: String,
    pub backend: SharedBackend,
    pub tools: Arc<ToolRegistry>,
    pub resources: Option<Arc<crate::resources::ResourceManager>>,
    pub system_prompt: Option<String>,
    /// Names of tools this agent is allowed to call. `None` means
    /// every tool registered on the world is in scope; `Some(set)`
    /// restricts both the schemas advertised to the model and the
    /// dispatcher's accept-list.
    allowed_tools: Option<HashSet<String>>,
    history: Mutex<Vec<ChatMessage>>,
}

impl AgentActor {
    pub fn new(
        id: ActorId,
        model: impl Into<String>,
        backend: SharedBackend,
        tools: Arc<ToolRegistry>,
    ) -> Self {
        Self {
            id,
            model: model.into(),
            backend,
            tools,
            resources: None,
            system_prompt: None,
            allowed_tools: None,
            history: Mutex::new(Vec::new()),
        }
    }

    pub fn with_system_prompt(mut self, prompt: impl Into<String>) -> Self {
        self.system_prompt = Some(prompt.into());
        self
    }

    pub fn with_resources(
        mut self,
        resources: Arc<crate::resources::ResourceManager>,
    ) -> Self {
        self.resources = Some(resources);
        self
    }

    /// Restrict this agent to a named subset of the world's tools. An
    /// empty list means "no tools" and is honoured as such; pass
    /// `None` (or skip this builder) to keep the unrestricted default.
    pub fn with_allowed_tools(mut self, names: impl IntoIterator<Item = String>) -> Self {
        self.allowed_tools = Some(names.into_iter().collect());
        self
    }

    fn allowed_schemas(&self) -> Vec<crate::backend::ToolSchema> {
        let all = self.tools.schemas();
        match &self.allowed_tools {
            None => all,
            Some(allow) => all.into_iter().filter(|s| allow.contains(&s.name)).collect(),
        }
    }

    fn is_allowed(&self, name: &str) -> bool {
        match &self.allowed_tools {
            None => true,
            Some(allow) => allow.contains(name),
        }
    }
}

#[async_trait]
impl Actor for AgentActor {
    fn id(&self) -> ActorId { self.id.clone() }
    fn kind(&self) -> ActorKind { ActorKind::Agent }

    async fn step(&self, bus: &Bus, envelope: Envelope) -> Result<(), CoreError> {
        let from = envelope.from.clone();
        let incoming = match envelope.message {
            Message::UserMessage { text } => text,
            Message::AgentMessage { text } => text,
            Message::ToolResult { name, result, is_error, .. } => {
                let prefix = if is_error { "tool error" } else { "tool" };
                format!("({prefix} {name} returned {result})")
            }
            Message::System { .. } | Message::ToolCall { .. } => return Ok(()),
        };
        self.history.lock().push(ChatMessage::user(incoming));

        // Standard tool-use loop: each iteration is one model turn.
        // The cap stops a model that keeps calling tools without ever
        // replying to the user. Eight is generous; most multi-step
        // plans resolve in three or four.
        const MAX_TOOL_TURNS: usize = 8;
        for _ in 0..MAX_TOOL_TURNS {
            let messages = self.history.lock().clone();
            let req = CompletionRequest {
                model: self.model.clone(),
                system: self.system_prompt.clone(),
                messages,
                tools: self.allowed_schemas(),
                ..Default::default()
            };
            let resp = match self.backend.complete(req).await {
                Ok(r) => r,
                Err(e) => {
                    bus.append_event(
                        Some(self.id.clone()),
                        EventPayload::System {
                            note: format!("backend error ({}): {e}", self.model),
                        },
                    )
                    .await;
                    return Ok(());
                }
            };
            record_completion_cost(bus, &self.id, &resp).await;

            if !resp.text.is_empty() {
                self.history
                    .lock()
                    .push(ChatMessage::assistant(resp.text.clone()));
                bus.send(
                    self.id.clone(),
                    Recipient::Actor(from.clone()),
                    Message::AgentMessage { text: resp.text },
                )
                .await?;
            }

            if resp.tool_calls.is_empty() {
                break;
            }

            for call in resp.tool_calls {
                let call_id = call.id.unwrap_or_else(|| MessageId::new().to_string());
                bus.append_event(
                    Some(self.id.clone()),
                    EventPayload::ToolCall {
                        id: call_id.clone(),
                        name: call.name.clone(),
                        args: call.args.clone(),
                        seed: false,
                    },
                )
                .await;
                if !self.is_allowed(&call.name) {
                    let err_json = serde_json::json!({
                        "ok": false,
                        "error": format!(
                            "tool {} is not in this agent's allowed set",
                            call.name
                        ),
                    });
                    self.history.lock().push(ChatMessage::tool(format!(
                        "tool {} blocked: not allowed for this agent",
                        call.name
                    )));
                    bus.append_event(
                        Some(self.id.clone()),
                        EventPayload::ToolResult {
                            id: call_id,
                            name: call.name,
                            result: err_json,
                            is_error: true,
                            seed: false,
                        },
                    )
                    .await;
                    continue;
                }
                let dispatch = self
                    .tools
                    .dispatch_async(&call.name, &call.args, self.resources.as_deref())
                    .await;
                // Flush any progress entries the tool emitted before
                // the outcome (so the trace renders progress, then
                // result).
                for entry in dispatch.progress {
                    bus.append_event(
                        Some(self.id.clone()),
                        EventPayload::Progress {
                            id: call_id.clone(),
                            tool: call.name.clone(),
                            fraction: entry.fraction,
                            message: entry.message,
                        },
                    )
                    .await;
                }
                if let Some(after) = dispatch.timed_out_after {
                    bus.append_event(
                        Some(self.id.clone()),
                        EventPayload::ToolTimeout {
                            id: call_id.clone(),
                            name: call.name.clone(),
                            after_ms: after.as_millis() as u64,
                        },
                    )
                    .await;
                }
                match dispatch.outcome {
                    Ok(outcome) => {
                        self.history.lock().push(ChatMessage::tool(format!(
                            "tool {} -> {}",
                            call.name, outcome.effect
                        )));
                        let costs = outcome.costs.clone();
                        bus.append_event(
                            Some(self.id.clone()),
                            EventPayload::ToolResult {
                                id: call_id.clone(),
                                name: call.name.clone(),
                                result: outcome.effect,
                                is_error: false,
                                seed: false,
                            },
                        )
                        .await;
                        if let Some(diff) = outcome.diff {
                            bus.append_event(
                                Some(self.id.clone()),
                                EventPayload::StateDiff { diff, seed: false },
                            )
                            .await;
                        }
                        for (unit, amount) in costs {
                            bus.record_cost(unit, amount, Some(self.id.clone())).await;
                        }
                    }
                    Err(e) => {
                        let err_json = serde_json::json!({
                            "ok": false,
                            "error": e.to_string(),
                        });
                        self.history.lock().push(ChatMessage::tool(format!(
                            "tool {} error: {e}",
                            call.name
                        )));
                        bus.append_event(
                            Some(self.id.clone()),
                            EventPayload::ToolResult {
                                id: call_id,
                                name: call.name,
                                result: err_json,
                                is_error: true,
                                seed: false,
                            },
                        )
                        .await;
                    }
                }
            }
        }
        Ok(())
    }
}
