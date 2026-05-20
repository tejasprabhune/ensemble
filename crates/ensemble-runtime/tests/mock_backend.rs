use std::sync::Arc;

use ensemble_core::bus::{Bus, Message, Recipient};
use ensemble_core::event::{EventLog, EventPayload};
use ensemble_core::ids::ActorId;
use ensemble_core::prelude::*;

use ensemble_runtime::{
    AgentActor, MockBackend, MockScript, MockTurn, ToolRegistry, UserActor,
};

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn agent_uses_tool_via_mock_backend() {
    let script = MockScript::new();
    script.push_for(
        "agent-model",
        MockTurn::tool("ping", serde_json::json!({"who": "alice"})),
    );
    script.push_for("agent-model", MockTurn::text("ack"));
    script.push_for("user-model", MockTurn::text("hi rep, please help"));
    let backend = Arc::new(MockBackend::new(script));

    let tools = ToolRegistry::new();
    tools.register(ensemble_runtime::Tool::new(
        "ping",
        "echo back the input",
        serde_json::json!({"type": "object"}),
        |args| Ok(serde_json::json!({"got": args.clone()})),
    ));
    let tools = Arc::new(tools);

    let log = EventLog::new();
    let bus = Bus::new(log.clone());

    let user_id = ActorId::from_label("alice");
    let agent_id = ActorId::from_label("rep");

    let user = Arc::new(UserActor::new(
        user_id.clone(),
        "user-model",
        backend.clone(),
    ));
    let agent = Arc::new(AgentActor::new(
        agent_id.clone(),
        "agent-model",
        backend.clone(),
        tools.clone(),
    ));

    let user_inbox = bus.register(user_id.clone()).await;
    let agent_inbox = bus.register(agent_id.clone()).await;

    let mut scheduler = Scheduler::new(
        bus.clone(),
        TickBudget { max_ticks: 40, max_events: 80, quiescence_ms: 200, drain_grace_ms: 100 },
    );
    scheduler.register(Arc::new(ensemble_core::actor::ActorHandle::new(user, user_inbox)));
    scheduler.register(Arc::new(ensemble_core::actor::ActorHandle::new(agent, agent_inbox)));
    scheduler.set_until(turn_count_exceeds(8)).await;

    bus.send(
        user_id.clone(),
        Recipient::Actor(agent_id.clone()),
        Message::UserMessage { text: "I need help with my ticket".into() },
    )
    .await
    .unwrap();

    scheduler.run().await.unwrap();

    let events = log.snapshot().await;
    let mut tool_calls = 0;
    let mut tool_results = 0;
    for e in &events {
        match &e.payload {
            EventPayload::ToolCall { .. } => tool_calls += 1,
            EventPayload::ToolResult { .. } => tool_results += 1,
            _ => {}
        }
    }
    assert!(tool_calls >= 1, "expected at least one tool call, got events: {events:?}");
    assert!(tool_results >= 1, "expected at least one tool result");
}
