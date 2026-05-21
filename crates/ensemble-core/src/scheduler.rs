use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::Mutex;
use tokio::task::JoinSet;

use crate::actor::ActorHandle;
use crate::bus::Bus;
use crate::error::CoreError;
use crate::event::{now_ms, Event, EventPayload};
use crate::ids::ActorId;
use crate::predicate::PredicateRegistry;
use crate::until::{Until, UntilCtx};

/// Why the scheduler stopped. All four are normal terminations; real
/// failures (bus closed, actor crashed) come back as `Err(CoreError)`
/// from `run`.
#[derive(Clone, Debug)]
pub enum StopReason {
    /// The until predicate returned true.
    UntilFired { label: String },
    /// No new events for `quiescence_ms`.
    Quiescent,
    /// Exhausted `max_ticks` or `max_events`. The cap that fired is
    /// reported so callers can tell the difference.
    BudgetExhausted {
        ticks: u64,
        events: usize,
        cap: BudgetCap,
    },
    /// A cost annotation pushed the world's running total past a
    /// declared budget. The unit, the amount of the offending
    /// annotation, and the configured budget are reported so
    /// scenarios can include them in failure messages.
    BudgetExceeded {
        unit: String,
        amount: f64,
        budget: f64,
    },
}

#[derive(Clone, Copy, Debug)]
pub enum BudgetCap {
    Ticks,
    Events,
}

/// Limits scheduler work to prevent runaway loops. Counts ticks (one
/// per bus event), total events processed, and a quiescence timeout
/// for when no new events arrive. Hitting any cap halts gracefully:
/// the scheduler appends a `System` event explaining which cap fired
/// and returns `Ok(StopReason::BudgetExhausted)`.
#[derive(Clone, Copy, Debug)]
pub struct TickBudget {
    pub max_ticks: u64,
    pub max_events: usize,
    pub quiescence_ms: u64,
    /// How long to keep running after the until predicate first
    /// fires, so in-flight actor steps can emit their follow-up
    /// events (a tool_call's matching tool_result, etc.). The
    /// scheduler halts as soon as the bus goes quiet for this long
    /// once it has entered the draining state.
    pub drain_grace_ms: u64,
}

impl Default for TickBudget {
    fn default() -> Self {
        Self {
            max_ticks: 200,
            max_events: 4000,
            quiescence_ms: 500,
            drain_grace_ms: 300,
        }
    }
}

pub struct Scheduler {
    bus: Bus,
    actors: HashMap<ActorId, Arc<ActorHandle>>,
    budget: TickBudget,
    until: Arc<Mutex<Option<Until>>>,
    predicates: Option<Arc<PredicateRegistry>>,
}

impl Scheduler {
    pub fn new(bus: Bus, budget: TickBudget) -> Self {
        Self {
            bus,
            actors: HashMap::new(),
            budget,
            until: Arc::new(Mutex::new(None)),
            predicates: None,
        }
    }

    pub fn register(&mut self, handle: Arc<ActorHandle>) {
        self.actors.insert(handle.id.clone(), handle);
    }

    pub fn bus(&self) -> &Bus {
        &self.bus
    }

    pub async fn set_until(&self, until: Until) {
        *self.until.lock().await = Some(until);
    }

    /// Make the world's named predicates visible to until-predicates
    /// the scheduler evaluates each tick. Wiring is opt-in so the
    /// existing turn-count-only path keeps working without a
    /// registry.
    pub fn with_predicates(mut self, predicates: Arc<PredicateRegistry>) -> Self {
        self.predicates = Some(predicates);
        self
    }

    /// Runs the scheduler until the `until` predicate fires, the
    /// budget is exhausted, or no new events arrive for the quiescence
    /// window. Each actor runs in its own tokio task draining its
    /// inbox; a watcher task wakes on each new bus event and re-checks
    /// the predicate and budget.
    pub async fn run(self) -> Result<StopReason, CoreError> {
        let Scheduler {
            bus,
            actors,
            budget,
            until,
            predicates,
        } = self;

        let log = bus.log().clone();
        let mut tasks: JoinSet<Result<(), CoreError>> = JoinSet::new();

        for handle in actors.values() {
            let actor = handle.actor.clone();
            let bus_clone = bus.clone();
            let inbox = handle.take_inbox().await.expect("inbox taken twice");
            tasks.spawn(actor_loop(actor, bus_clone, inbox));
        }

        let notifier = bus.notifier();
        let watcher_bus = bus.clone();
        let watcher_log = log.clone();
        let watcher_until = until.clone();
        let watcher = tokio::spawn(async move {
            // Track when the until predicate first fired so we can
            // give in-flight steps a small grace window to finish
            // (e.g. emit the trailing tool_result for a tool_call).
            let mut drain_label: Option<String> = None;
            loop {
                if let Some(halt) = watcher_bus.halt_reason().await {
                    return Ok::<_, CoreError>(StopReason::BudgetExceeded {
                        unit: halt.unit,
                        amount: halt.amount,
                        budget: halt.budget,
                    });
                }
                let cur = watcher_log.len().await as u64;
                watcher_bus.set_tick(cur).await;
                if cur >= budget.max_ticks {
                    return Ok::<_, CoreError>(StopReason::BudgetExhausted {
                        ticks: cur,
                        events: cur as usize,
                        cap: BudgetCap::Ticks,
                    });
                }
                if cur as usize >= budget.max_events {
                    return Ok(StopReason::BudgetExhausted {
                        ticks: cur,
                        events: cur as usize,
                        cap: BudgetCap::Events,
                    });
                }
                if drain_label.is_none() {
                    // Snapshot the trace here so until-predicates that
                    // ask the predicate registry can walk events
                    // synchronously inside their `check` closure
                    // without re-locking the live log.
                    let snapshot = watcher_log.snapshot().await;
                    let label = {
                        let guard = watcher_until.lock().await;
                        let ctx = UntilCtx {
                            tick: cur,
                            log: &watcher_log,
                            events_seen: cur as usize,
                            trace: Some(snapshot.as_slice()),
                            predicates: predicates.as_ref(),
                        };
                        match guard.as_ref() {
                            Some(u) if u.check(&ctx) => Some(u.label.clone()),
                            _ => None,
                        }
                    };
                    if let Some(label) = label {
                        drain_label = Some(label);
                    }
                }
                let wait = notifier.notified();
                tokio::pin!(wait);
                let sleep_ms = if drain_label.is_some() {
                    budget.drain_grace_ms
                } else {
                    budget.quiescence_ms
                };
                let timeout = tokio::time::sleep(std::time::Duration::from_millis(sleep_ms));
                tokio::pin!(timeout);
                tokio::select! {
                    _ = &mut wait => continue,
                    _ = &mut timeout => {
                        if let Some(label) = drain_label {
                            return Ok(StopReason::UntilFired { label });
                        }
                        return Ok(StopReason::Quiescent);
                    }
                }
            }
        });

        let outcome = watcher
            .await
            .map_err(|e| CoreError::SchedulerExit(e.to_string()))??;

        tasks.shutdown().await;

        let note = match &outcome {
            StopReason::UntilFired { label } => {
                format!("until predicate fired ({label}); halting")
            }
            StopReason::Quiescent => "scheduler quiescent; halting".into(),
            StopReason::BudgetExhausted { ticks, events, cap } => format!(
                "tick budget exhausted at {ticks} ticks / {events} events (cap: {}); halting",
                match cap {
                    BudgetCap::Ticks => "max_ticks",
                    BudgetCap::Events => "max_events",
                }
            ),
            StopReason::BudgetExceeded {
                unit,
                amount,
                budget,
            } => format!("budget exceeded: {unit} {amount} > {budget}; halting"),
        };
        let tick = bus.current_tick().await;
        log.append(Event {
            tick,
            ts_ms: now_ms(),
            actor: None,
            message_id: None,
            payload: EventPayload::System { note },
        })
        .await;

        Ok(outcome)
    }
}

async fn actor_loop(
    actor: Arc<dyn crate::actor::Actor>,
    bus: Bus,
    mut inbox: tokio::sync::mpsc::Receiver<crate::bus::Envelope>,
) -> Result<(), CoreError> {
    while let Some(env) = inbox.recv().await {
        actor.step(&bus, env).await?;
    }
    Ok(())
}
