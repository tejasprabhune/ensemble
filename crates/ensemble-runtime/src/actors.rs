use async_trait::async_trait;
use parking_lot::Mutex;
use std::sync::Arc;

use ensemble_core::actor::{Actor, ActorKind};
use ensemble_core::bus::{Bus, Envelope, Message, Recipient};
use ensemble_core::error::CoreError;
use ensemble_core::event::EventPayload;
use ensemble_core::ids::ActorId;

use crate::backend::{ChatMessage, CompletionRequest, SharedBackend};
use crate::tools::ToolRegistry;

/// A simulated end-user driven by an LLM. Receives agent messages,
/// asks the backend for a reply, and posts that reply on the bus
/// (broadcast so any agent in the same room can hear it).
pub struct UserActor {
    pub id: ActorId,
    pub model: String,
    pub backend: SharedBackend,
    history: Mutex<Vec<ChatMessage>>,
}

impl UserActor {
    pub fn new(id: ActorId, model: impl Into<String>, backend: SharedBackend) -> Self {
        Self {
            id,
            model: model.into(),
            backend,
            history: Mutex::new(Vec::new()),
        }
    }
}

#[async_trait]
impl Actor for UserActor {
    fn id(&self) -> ActorId { self.id.clone() }
    fn kind(&self) -> ActorKind { ActorKind::User }

    async fn step(&self, bus: &Bus, envelope: Envelope) -> Result<(), CoreError> {
        let incoming = match envelope.message {
            Message::AgentMessage { text } => text,
            Message::UserMessage { text } => text,
            Message::System { .. } | Message::ToolCall { .. } | Message::ToolResult { .. } => {
                return Ok(());
            }
        };
        {
            let mut h = self.history.lock();
            h.push(ChatMessage::user(incoming));
        }
        let messages = self.history.lock().clone();
        let req = CompletionRequest {
            model: self.model.clone(),
            messages,
            ..Default::default()
        };
        let resp = self
            .backend
            .complete(req)
            .await
            .map_err(|e| CoreError::Other(format!("backend: {e}")))?;
        self.history.lock().push(ChatMessage::assistant(resp.text.clone()));
        if !resp.text.is_empty() {
            bus.send(
                self.id.clone(),
                Recipient::Broadcast,
                Message::UserMessage { text: resp.text },
            )
            .await?;
        }
        Ok(())
    }
}

/// An LLM-driven agent that can issue tool calls. Tool calls are
/// dispatched through the registry; the resulting JSON effect is fed
/// back to the model on the next turn.
pub struct AgentActor {
    pub id: ActorId,
    pub model: String,
    pub backend: SharedBackend,
    pub tools: Arc<ToolRegistry>,
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
            history: Mutex::new(Vec::new()),
        }
    }
}

#[async_trait]
impl Actor for AgentActor {
    fn id(&self) -> ActorId { self.id.clone() }
    fn kind(&self) -> ActorKind { ActorKind::Agent }

    async fn step(&self, bus: &Bus, envelope: Envelope) -> Result<(), CoreError> {
        let incoming = match envelope.message {
            Message::UserMessage { text } => text,
            Message::AgentMessage { text } => text,
            Message::ToolResult { name, result } => {
                format!("(tool {name} returned {result})")
            }
            Message::System { .. } | Message::ToolCall { .. } => return Ok(()),
        };
        {
            let mut h = self.history.lock();
            h.push(ChatMessage::user(incoming));
        }
        let messages = self.history.lock().clone();
        let req = CompletionRequest {
            model: self.model.clone(),
            messages,
            tools: self.tools.schemas(),
            ..Default::default()
        };
        let resp = self
            .backend
            .complete(req)
            .await
            .map_err(|e| CoreError::Other(format!("backend: {e}")))?;

        if !resp.text.is_empty() {
            self.history.lock().push(ChatMessage::assistant(resp.text.clone()));
            bus.send(
                self.id.clone(),
                Recipient::Broadcast,
                Message::AgentMessage { text: resp.text },
            )
            .await?;
        }

        for call in resp.tool_calls {
            bus.append_event(
                Some(self.id.clone()),
                EventPayload::ToolCall {
                    name: call.name.clone(),
                    args: call.args.clone(),
                },
            )
            .await;
            match self.tools.dispatch(&call.name, &call.args) {
                Ok(effect) => {
                    self.history.lock().push(ChatMessage::tool(format!(
                        "tool {} -> {}",
                        call.name, effect
                    )));
                    bus.append_event(
                        Some(self.id.clone()),
                        EventPayload::ToolResult {
                            name: call.name,
                            result: effect,
                        },
                    )
                    .await;
                }
                Err(e) => {
                    bus.append_event(
                        Some(self.id.clone()),
                        EventPayload::System {
                            note: format!("tool error: {e}"),
                        },
                    )
                    .await;
                }
            }
        }
        Ok(())
    }
}
