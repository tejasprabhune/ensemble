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
    UserMessage {
        text: String,
    },
    AgentMessage {
        text: String,
    },
    /// A tool invocation. `id` lets a later `ToolResult` be paired with
    /// the call that produced it; provider-supplied ids (Anthropic
    /// `tool_use.id`, OpenAI `tool_calls[].id`) are preserved when
    /// available and otherwise generated.
    ToolCall {
        id: String,
        name: String,
        args: serde_json::Value,
    },
    /// Result of a tool invocation. `is_error` is set when the tool
    /// failed; the calling agent reads `result` either way and replies
    /// to the user with whatever it makes of the response.
    ToolResult {
        id: String,
        name: String,
        result: serde_json::Value,
        #[serde(default)]
        is_error: bool,
    },
    System {
        note: String,
    },
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
    halt: Arc<Mutex<Option<HaltReason>>>,
    costs: Arc<Mutex<CostState>>,
}

#[derive(Default, Clone, Debug)]
struct CostState {
    totals: std::collections::HashMap<String, f64>,
    budgets: std::collections::HashMap<String, f64>,
}

/// A request to halt the scheduler from outside the watcher's
/// predicate / budget machinery. Set by world-level checks (the
/// budget tracker calls `Bus::halt_with` when a unit exceeds its
/// declared cap).
#[derive(Clone, Debug)]
pub struct HaltReason {
    pub unit: String,
    pub amount: f64,
    pub budget: f64,
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
            halt: Arc::new(Mutex::new(None)),
            costs: Arc::new(Mutex::new(CostState::default())),
        }
    }

    /// Declare a budget cap for `unit`. When [`record_cost`] would
    /// push the running total past this, the bus halts the scheduler
    /// (via [`halt_with`]) with `BudgetExceeded`.
    pub async fn set_budget(&self, unit: impl Into<String>, amount: f64) {
        self.costs.lock().await.budgets.insert(unit.into(), amount);
    }

    pub async fn cost_total(&self, unit: &str) -> f64 {
        *self
            .costs
            .lock()
            .await
            .totals
            .get(unit)
            .unwrap_or(&0.0)
    }

    /// Record a cost annotation. Updates the running total, appends
    /// a `Cost` event to the log, and signals the scheduler to halt
    /// if a budget cap has been crossed.
    pub async fn record_cost(&self, unit: impl Into<String>, amount: f64) {
        let unit = unit.into();
        let (new_total, budget) = {
            let mut state = self.costs.lock().await;
            let total = state.totals.entry(unit.clone()).or_insert(0.0);
            *total += amount;
            let new_total = *total;
            let budget = state.budgets.get(&unit).copied();
            (new_total, budget)
        };
        self.append_event(
            None,
            EventPayload::Cost {
                unit: unit.clone(),
                amount,
                running_total: new_total,
            },
        )
        .await;
        if let Some(cap) = budget {
            if new_total > cap {
                self.halt_with(HaltReason {
                    unit,
                    amount: new_total,
                    budget: cap,
                })
                .await;
            }
        }
    }

    /// Signal the scheduler to halt with the supplied reason. The
    /// watcher checks the flag on every tick and returns
    /// `StopReason::BudgetExceeded` when it fires.
    pub async fn halt_with(&self, reason: HaltReason) {
        *self.halt.lock().await = Some(reason);
        self.notify.notify_waiters();
    }

    pub async fn halt_reason(&self) -> Option<HaltReason> {
        self.halt.lock().await.clone()
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
        Message::ToolCall { id, name, args } => EventPayload::ToolCall {
            id: id.clone(),
            name: name.clone(),
            args: args.clone(),
        },
        Message::ToolResult {
            id,
            name,
            result,
            is_error,
        } => EventPayload::ToolResult {
            id: id.clone(),
            name: name.clone(),
            result: result.clone(),
            is_error: *is_error,
        },
        Message::System { note } => EventPayload::System { note: note.clone() },
    }
}
