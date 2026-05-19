use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::{mpsc, Mutex};

use crate::error::CoreError;
use crate::event::{now_ms, Event, EventLog, EventPayload, Tick};
use crate::ids::{ActorId, MessageId};

/// Messages flowing on the bus. Untyped at the wire level so any actor
/// can receive any payload; routing decides who actually gets each one.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub enum Message {
    UserMessage { text: String },
    AgentMessage { text: String },
    ToolCall { name: String, args: serde_json::Value },
    ToolResult { name: String, result: serde_json::Value },
    System { note: String },
}

#[derive(Clone, Debug)]
pub struct Envelope {
    pub id: MessageId,
    pub from: ActorId,
    pub to: Recipient,
    pub tick: Tick,
    pub message: Message,
}

#[derive(Clone, Debug)]
pub enum Recipient {
    /// Direct point-to-point delivery to a single actor.
    Actor(ActorId),
    /// Broadcast: every actor except the sender receives a copy.
    Broadcast,
}

#[derive(Clone)]
pub struct Bus {
    inner: Arc<Mutex<BusInner>>,
    log: EventLog,
}

struct BusInner {
    inboxes: HashMap<ActorId, mpsc::Sender<Envelope>>,
    tick: Tick,
}

impl Bus {
    pub fn new(log: EventLog) -> Self {
        Self {
            inner: Arc::new(Mutex::new(BusInner {
                inboxes: HashMap::new(),
                tick: 0,
            })),
            log,
        }
    }

    pub fn log(&self) -> &EventLog {
        &self.log
    }

    pub async fn register(&self, actor: ActorId) -> mpsc::Receiver<Envelope> {
        let (tx, rx) = mpsc::channel(64);
        let mut inner = self.inner.lock().await;
        inner.inboxes.insert(actor, tx);
        rx
    }

    pub async fn current_tick(&self) -> Tick {
        self.inner.lock().await.tick
    }

    pub async fn advance_tick(&self) -> Tick {
        let mut inner = self.inner.lock().await;
        inner.tick += 1;
        inner.tick
    }

    pub async fn send(
        &self,
        from: ActorId,
        to: Recipient,
        message: Message,
    ) -> Result<(), CoreError> {
        let inner = self.inner.lock().await;
        let tick = inner.tick;
        let id = MessageId::new();
        let envelope = Envelope {
            id: id.clone(),
            from: from.clone(),
            to: to.clone(),
            tick,
            message: message.clone(),
        };
        self.log
            .append(Event {
                tick,
                ts_ms: now_ms(),
                actor: Some(from.clone()),
                message_id: Some(id),
                payload: payload_for(&message),
            })
            .await;
        match to {
            Recipient::Actor(target) => {
                if let Some(tx) = inner.inboxes.get(&target) {
                    tx.send(envelope).await.map_err(|_| CoreError::BusClosed)?;
                } else {
                    return Err(CoreError::ActorNotFound(target.to_string()));
                }
            }
            Recipient::Broadcast => {
                for (id, tx) in inner.inboxes.iter() {
                    if id == &from {
                        continue;
                    }
                    let _ = tx.send(envelope.clone()).await;
                }
            }
        }
        Ok(())
    }
}

fn payload_for(message: &Message) -> EventPayload {
    match message {
        Message::UserMessage { text } => EventPayload::UserMessage { text: text.clone() },
        Message::AgentMessage { text } => EventPayload::AgentMessage { text: text.clone() },
        Message::ToolCall { name, args } => EventPayload::ToolCall {
            name: name.clone(),
            args: args.clone(),
        },
        Message::ToolResult { name, result } => EventPayload::ToolResult {
            name: name.clone(),
            result: result.clone(),
        },
        Message::System { note } => EventPayload::System { note: note.clone() },
    }
}
