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
    /// Direct point-to-point delivery to a single actor. The enum
    /// wrapper is retained (rather than collapsing to a bare
    /// `ActorId`) so message routing can grow further variants
    /// later (group ids, role-based targets) without a churning
    /// every call site.
    Actor(ActorId),
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
    /// World-wide running totals per unit.
    totals: std::collections::HashMap<String, f64>,
    /// World-wide caps per unit.
    budgets: std::collections::HashMap<String, f64>,
    /// Per-actor running totals: actor_id -> unit -> total.
    actor_totals: std::collections::HashMap<ActorId, std::collections::HashMap<String, f64>>,
    /// Per-actor caps: actor_id -> unit -> cap.
    actor_budgets: std::collections::HashMap<ActorId, std::collections::HashMap<String, f64>>,
}

/// A request to halt the scheduler from outside the watcher's
/// predicate / budget machinery. Set by world-level checks (the
/// budget tracker calls `Bus::halt_with` when a unit exceeds its
/// declared cap). `actor` is set when the cap that fired was scoped
/// to a single actor; `None` means the world-wide cap fired.
#[derive(Clone, Debug)]
pub struct HaltReason {
    pub unit: String,
    pub amount: f64,
    pub budget: f64,
    pub actor: Option<ActorId>,
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

    /// Declare a world-wide budget cap for `unit`. When [`record_cost`]
    /// would push the world-wide running total past this, the bus halts
    /// the scheduler (via [`halt_with`]) with `BudgetExceeded`.
    pub async fn set_budget(&self, unit: impl Into<String>, amount: f64) {
        self.costs.lock().await.budgets.insert(unit.into(), amount);
    }

    /// Declare a per-actor budget cap. Same semantics as `set_budget`
    /// but checked against the actor's own running total, not the
    /// world-wide total. A cost recorded against a different actor
    /// does not consume this cap.
    pub async fn set_actor_budget(
        &self,
        actor: ActorId,
        unit: impl Into<String>,
        amount: f64,
    ) {
        let mut state = self.costs.lock().await;
        state
            .actor_budgets
            .entry(actor)
            .or_default()
            .insert(unit.into(), amount);
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

    pub async fn actor_cost_total(&self, actor: &ActorId, unit: &str) -> f64 {
        let state = self.costs.lock().await;
        state
            .actor_totals
            .get(actor)
            .and_then(|m| m.get(unit))
            .copied()
            .unwrap_or(0.0)
    }

    /// Record a cost annotation. Updates the world-wide running total
    /// (and the per-actor total when `actor` is supplied), appends a
    /// `Cost` event to the log, and signals the scheduler to halt if
    /// any cap (world-wide or per-actor) has been crossed.
    pub async fn record_cost(
        &self,
        unit: impl Into<String>,
        amount: f64,
        actor: Option<ActorId>,
    ) {
        let unit = unit.into();
        let (new_total, world_budget, actor_total, actor_budget) = {
            let mut state = self.costs.lock().await;
            let total = state.totals.entry(unit.clone()).or_insert(0.0);
            *total += amount;
            let new_total = *total;
            let world_budget = state.budgets.get(&unit).copied();
            let (actor_total, actor_budget) = if let Some(ref a) = actor {
                let totals = state.actor_totals.entry(a.clone()).or_default();
                let entry = totals.entry(unit.clone()).or_insert(0.0);
                *entry += amount;
                let actor_total = *entry;
                let actor_budget = state
                    .actor_budgets
                    .get(a)
                    .and_then(|m| m.get(&unit))
                    .copied();
                (Some(actor_total), actor_budget)
            } else {
                (None, None)
            };
            (new_total, world_budget, actor_total, actor_budget)
        };
        self.append_event(
            actor.clone(),
            EventPayload::Cost {
                unit: unit.clone(),
                amount,
                running_total: new_total,
            },
        )
        .await;
        if let (Some(at), Some(cap)) = (actor_total, actor_budget) {
            if at > cap {
                self.halt_with(HaltReason {
                    unit: unit.clone(),
                    amount: at,
                    budget: cap,
                    actor: actor.clone(),
                })
                .await;
                return;
            }
        }
        if let Some(cap) = world_budget {
            if new_total > cap {
                self.halt_with(HaltReason {
                    unit,
                    amount: new_total,
                    budget: cap,
                    actor: None,
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
        let send_result: Result<(), CoreError> = {
            let Recipient::Actor(target) = to;
            if let Some(tx) = inner.inboxes.get(&target) {
                tx.send(envelope).await.map_err(|_| CoreError::BusClosed)
            } else {
                Err(CoreError::ActorNotFound(target.to_string()))
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
            seed: false,
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
            seed: false,
        },
        Message::System { note } => EventPayload::System { note: note.clone() },
    }
}
