use std::sync::Arc;

use async_trait::async_trait;
use ensemble_core::bus::{Message, Recipient};
use ensemble_core::prelude::*;

struct EchoActor {
    id: ActorId,
    reply_to: ActorId,
}

#[async_trait]
impl ensemble_core::actor::Actor for EchoActor {
    fn id(&self) -> ActorId {
        self.id.clone()
    }

    fn kind(&self) -> ActorKind {
        ActorKind::Agent
    }

    async fn step(
        &self,
        bus: &Bus,
        envelope: ensemble_core::bus::Envelope,
    ) -> Result<(), CoreError> {
        // Echo whatever text we saw back to the configured partner.
        let text = match envelope.message {
            Message::UserMessage { text } => text,
            Message::AgentMessage { text } => text,
            _ => return Ok(()),
        };
        bus.send(
            self.id.clone(),
            Recipient::Actor(self.reply_to.clone()),
            Message::AgentMessage {
                text: format!("echo: {text}"),
            },
        )
        .await
    }
}

async fn register(
    bus: &Bus,
    id: ActorId,
    reply_to: ActorId,
) -> Arc<ensemble_core::actor::ActorHandle> {
    let inbox = bus.register(id.clone()).await;
    let actor = Arc::new(EchoActor { id, reply_to });
    Arc::new(ensemble_core::actor::ActorHandle::new(actor, inbox))
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn ping_pong_runs_until_budget_or_predicate() {
    let log = EventLog::new();
    let bus = Bus::new(log.clone());

    let ping = ActorId::from_label("ping");
    let pong = ActorId::from_label("pong");

    let mut scheduler = Scheduler::new(
        bus.clone(),
        TickBudget {
            max_ticks: 20,
            max_events: 200,
            quiescence_ms: 500,
            drain_grace_ms: 200,
        },
    );
    scheduler.register(register(&bus, ping.clone(), pong.clone()).await);
    scheduler.register(register(&bus, pong.clone(), ping.clone()).await);

    // Stop after 8 messages have been logged.
    scheduler.set_until(turn_count_exceeds(8)).await;

    // Seed a single message; the echo bounces will do the rest.
    bus.send(
        ping.clone(),
        Recipient::Actor(pong.clone()),
        Message::UserMessage { text: "hi".into() },
    )
    .await
    .unwrap();

    scheduler.run().await.unwrap();

    let events = log.snapshot().await;
    assert!(
        events.len() >= 8,
        "expected at least 8 events, got {}",
        events.len()
    );
    let agent_count = events
        .iter()
        .filter(|e| matches!(e.payload, EventPayload::AgentMessage { .. }))
        .count();
    assert!(
        agent_count >= 7,
        "expected at least 7 echo replies, got {agent_count}"
    );
}
