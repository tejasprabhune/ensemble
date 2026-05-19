use ensemble_core::prelude::*;
use ensemble_core::bus::Bus;
use ensemble_core::error::{RestoreError, ToolError};
use ensemble_core::event::EventPayload;
use serde::{Deserialize, Serialize};

#[derive(Default, Clone, Debug)]
struct Counter {
    value: i64,
    history: Vec<String>,
}

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
enum CounterCall {
    Inc { by: i64, note: String },
    Reset,
}

#[derive(Serialize, Clone)]
struct CounterEffect {
    new_value: i64,
}

#[derive(Serialize)]
struct CounterDiff {
    field: &'static str,
    old: i64,
    new: i64,
    note: String,
}

#[derive(Serialize, Deserialize)]
struct CounterSnapshot {
    value: i64,
    history: Vec<String>,
}

impl WorldState for Counter {
    type ToolCall = CounterCall;
    type ToolEffect = CounterEffect;
    type Diff = CounterDiff;

    fn apply(
        &mut self,
        call: Self::ToolCall,
    ) -> Result<(Self::ToolEffect, Self::Diff), ToolError> {
        match call {
            CounterCall::Inc { by, note } => {
                let old = self.value;
                self.value += by;
                self.history.push(note.clone());
                Ok((
                    CounterEffect { new_value: self.value },
                    CounterDiff {
                        field: "value",
                        old,
                        new: self.value,
                        note,
                    },
                ))
            }
            CounterCall::Reset => {
                let old = self.value;
                self.value = 0;
                self.history.push("reset".into());
                Ok((
                    CounterEffect { new_value: 0 },
                    CounterDiff {
                        field: "value",
                        old,
                        new: 0,
                        note: "reset".into(),
                    },
                ))
            }
        }
    }

    fn snapshot(&self) -> Vec<u8> {
        serde_json::to_vec(&CounterSnapshot {
            value: self.value,
            history: self.history.clone(),
        })
        .unwrap()
    }

    fn restore(&mut self, snapshot: &[u8]) -> Result<(), RestoreError> {
        let snap: CounterSnapshot = serde_json::from_slice(snapshot)
            .map_err(|e| RestoreError::Decode(e.to_string()))?;
        self.value = snap.value;
        self.history = snap.history;
        Ok(())
    }
}

#[tokio::test]
async fn apply_emits_tool_result_and_diff() {
    let log = EventLog::new();
    let bus = Bus::new(log.clone());
    let world = WorldHandle::new(Counter::default());
    let actor = ActorId::from_label("agent");

    let eff = world
        .apply_and_log(
            &bus,
            actor.clone(),
            "inc",
            CounterCall::Inc { by: 5, note: "first".into() },
        )
        .await
        .unwrap();
    assert_eq!(eff.new_value, 5);

    let events = log.snapshot().await;
    assert_eq!(events.len(), 2);
    assert!(matches!(events[0].payload, EventPayload::ToolResult { .. }));
    match &events[1].payload {
        EventPayload::StateDiff { diff } => {
            assert_eq!(diff["field"], "value");
            assert_eq!(diff["old"], 0);
            assert_eq!(diff["new"], 5);
        }
        other => panic!("expected StateDiff, got {other:?}"),
    }
}

#[tokio::test]
async fn snapshot_and_restore_round_trip() {
    let world = WorldHandle::new(Counter::default());
    world
        .apply(CounterCall::Inc { by: 7, note: "a".into() })
        .await
        .unwrap();
    let snap = world.snapshot().await;
    world
        .apply(CounterCall::Inc { by: 100, note: "b".into() })
        .await
        .unwrap();
    let post = world.with(|c| c.value).await;
    assert_eq!(post, 107);

    world.restore(&snap).await.unwrap();
    let restored = world.with(|c| (c.value, c.history.clone())).await;
    assert_eq!(restored.0, 7);
    assert_eq!(restored.1, vec!["a".to_string()]);
}

#[tokio::test]
async fn until_combinators_compose() {
    let log = EventLog::new();
    let bus = Bus::new(log.clone());
    let actor = ActorId::from_label("agent");
    bus.append_event(
        Some(actor),
        EventPayload::System { note: "tick".into() },
    )
    .await;

    let u = ensemble_core::until::all_of(vec![
        ensemble_core::until::any_of(vec![
            ensemble_core::until::turn_count_exceeds(10),
            ensemble_core::until::turn_count_exceeds(1),
        ]),
        ensemble_core::until::turn_count_exceeds(0),
    ]);
    let ctx = ensemble_core::until::UntilCtx {
        tick: 1,
        log: &log,
        events_seen: 1,
    };
    assert!(u.check(&ctx));

    let u2 = ensemble_core::until::all_of(vec![
        ensemble_core::until::turn_count_exceeds(2),
        ensemble_core::until::turn_count_exceeds(0),
    ]);
    assert!(!u2.check(&ctx));
}
