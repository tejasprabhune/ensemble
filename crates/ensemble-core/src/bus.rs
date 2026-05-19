use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::{mpsc, Mutex, Notify};

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
    notify: Arc<Notify>,
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
            notify: Arc::new(Notify::new()),
        }
    }

    pub fn log(&self) -> &EventLog {
        &self.log
    }

    /// Wake any task waiting via `wait_for_activity()`. Called internally
    /// each time the bus appends to the log.
    pub fn notifier(&self) -> Arc<Notify> {
        self.notify.clone()
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

    /// Set the tick counter directly. Used by the scheduler to keep tick
    /// equal to the count of bus events.
    pub async fn set_tick(&self, tick: Tick) {
        self.inner.lock().await.tick = tick;
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
        let send_result: Result<(), CoreError> = match to {
            Recipient::Actor(target) => {
                if let Some(tx) = inner.inboxes.get(&target) {
                    tx.send(envelope).await.map_err(|_| CoreError::BusClosed)
                } else {
                    Err(CoreError::ActorNotFound(target.to_string()))
                }
            }
            Recipient::Broadcast => {
                for (id, tx) in inner.inboxes.iter() {
                    if id == &from {
                        continue;
                    }
                    let _ = tx.send(envelope.clone()).await;
                }
                Ok(())
            }
        };
        drop(inner);
        self.notify.notify_waiters();
        send_result
    }

    /// Append a non-message event (state diff, system note) to the log
    /// without routing anything to an inbox. Tick is whatever the bus
    /// currently has.
    pub async fn append_event(&self, actor: Option<ActorId>, payload: EventPayload) {
        let tick = self.current_tick().await;
        self.log
            .append(Event {
                tick,
                ts_ms: now_ms(),
                actor,
                message_id: None,
                payload,
            })
            .await;
        self.notify.notify_waiters();
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
