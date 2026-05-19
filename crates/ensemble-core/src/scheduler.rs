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

/// Limits scheduler work to prevent runaway loops. Counts ticks (one
/// per bus event), total events processed, and a quiescence timeout
/// for when no new events arrive. Hitting `max_ticks` or `max_events`
/// halts with `TickBudgetExhausted`; quiescence halts cleanly.
#[derive(Clone, Copy, Debug)]
pub struct TickBudget {
    pub max_ticks: u64,
    pub max_events: usize,
    pub quiescence_ms: u64,
}

impl Default for TickBudget {
    fn default() -> Self {
        Self {
            max_ticks: 200,
            max_events: 4000,
            quiescence_ms: 500,
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
    /// task draining its inbox; a watcher task wakes on each new bus
    /// event and re-checks the `until` predicate.
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
            let bus_clone = bus.clone();
            let inbox = handle.take_inbox().await.expect("inbox taken twice");
            tasks.spawn(actor_loop(actor, bus_clone, inbox));
        }

        let notifier = bus.notifier();
        let watcher_bus = bus.clone();
        let watcher_log = log.clone();
        let watcher_until = until.clone();
        let watcher = tokio::spawn(async move {
            loop {
                // Check once before blocking so any messages already in
                // the log are considered before we wait for new ones.
                let cur = watcher_log.len().await as u64;
                watcher_bus.set_tick(cur).await;
                if cur >= budget.max_ticks || cur as usize >= budget.max_events {
                    return Err(CoreError::TickBudgetExhausted);
                }
                let ctx = UntilCtx {
                    tick: cur,
                    log: &watcher_log,
                    events_seen: cur as usize,
                };
                let stop = {
                    let guard = watcher_until.lock().await;
                    guard.as_ref().map(|u| u.check(&ctx)).unwrap_or(false)
                };
                if stop {
                    return Ok::<_, CoreError>(StopReason::UntilFired);
                }
                let wait = notifier.notified();
                tokio::pin!(wait);
                let timeout = tokio::time::sleep(std::time::Duration::from_millis(
                    budget.quiescence_ms,
                ));
                tokio::pin!(timeout);
                tokio::select! {
                    _ = &mut wait => continue,
                    _ = &mut timeout => {
                        return Ok(StopReason::Quiescent);
                    }
                }
            }
        });

        let outcome = watcher
            .await
            .map_err(|e| CoreError::SchedulerExit(e.to_string()))??;

        tasks.shutdown().await;

        let note = match outcome {
            StopReason::UntilFired => "until predicate fired; halting",
            StopReason::Quiescent => "scheduler quiescent; halting",
        };
        let tick = bus.current_tick().await;
        log.append(Event {
            tick,
            ts_ms: now_ms(),
            actor: None,
            message_id: None,
            payload: EventPayload::System { note: note.into() },
        })
        .await;

        Ok(())
    }
}

enum StopReason {
    UntilFired,
    Quiescent,
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
