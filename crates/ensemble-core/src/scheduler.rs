use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::Mutex;
use tokio::task::JoinSet;

use crate::actor::ActorHandle;
use crate::bus::Bus;
use crate::error::CoreError;
use crate::event::{now_ms, Event, EventPayload};
use crate::ids::ActorId;
use crate::until::{Until, UntilCtx};

/// Limits scheduler work to prevent runaway loops. Counts both ticks
/// and events processed; whichever cap fires first halts the run with
/// `TickBudgetExhausted`.
#[derive(Clone, Copy, Debug)]
pub struct TickBudget {
    pub max_ticks: u64,
    pub max_events: usize,
}

impl Default for TickBudget {
    fn default() -> Self {
        Self {
            max_ticks: 200,
            max_events: 4000,
        }
    }
}

pub struct Scheduler {
    bus: Bus,
    actors: HashMap<ActorId, Arc<ActorHandle>>,
    budget: TickBudget,
    until: Arc<Mutex<Option<Until>>>,
}

impl Scheduler {
    pub fn new(bus: Bus, budget: TickBudget) -> Self {
        Self {
            bus,
            actors: HashMap::new(),
            budget,
            until: Arc::new(Mutex::new(None)),
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

    /// Runs the scheduler until the `until` predicate fires or the
    /// tick budget is exhausted. Each actor runs in its own tokio
    /// task draining its inbox; the scheduler watches the event log
    /// and advances the tick counter each time any actor produces a
    /// message.
    pub async fn run(self) -> Result<(), CoreError> {
        let Scheduler {
            bus,
            actors,
            budget,
            until,
        } = self;

        let log = bus.log().clone();
        let mut tasks: JoinSet<Result<(), CoreError>> = JoinSet::new();

        for handle in actors.values() {
            let actor = handle.actor.clone();
            let bus = bus.clone();
            let inbox = handle.take_inbox().await.expect("inbox taken twice");
            tasks.spawn(actor_loop(actor, bus, inbox));
        }

        let watcher_bus = bus.clone();
        let watcher_log = log.clone();
        let watcher_until = until.clone();
        let watcher = tokio::spawn(async move {
            let mut last_seen = 0usize;
            loop {
                tokio::time::sleep(std::time::Duration::from_millis(5)).await;
                let cur = watcher_log.len().await;
                if cur > last_seen {
                    last_seen = cur;
                    let tick = watcher_bus.advance_tick().await;
                    let ctx = UntilCtx {
                        tick,
                        log: &watcher_log,
                        events_seen: cur,
                    };
                    let stop = {
                        let guard = watcher_until.lock().await;
                        guard.as_ref().map(|u| u.check(&ctx)).unwrap_or(false)
                    };
                    if stop {
                        return Ok::<_, CoreError>(StopReason::UntilFired);
                    }
                    if tick >= budget.max_ticks || cur >= budget.max_events {
                        return Err(CoreError::TickBudgetExhausted);
                    }
                }
            }
        });

        let outcome = watcher.await.map_err(|e| CoreError::SchedulerExit(e.to_string()))??;

        tasks.shutdown().await;

        if matches!(outcome, StopReason::UntilFired) {
            log.append(Event {
                tick: bus.current_tick().await,
                ts_ms: now_ms(),
                actor: None,
                message_id: None,
                payload: EventPayload::System {
                    note: "until predicate fired; halting".into(),
                },
            })
            .await;
        }

        Ok(())
    }
}

enum StopReason {
    UntilFired,
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
