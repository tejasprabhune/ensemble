"""World.apply: the python entry point for system-level state mutations.

A python plugin world's tools can be invoked through world.apply
without going through an actor, mirroring the rust
WorldHandle::apply_and_log path. The runtime emits ToolCall,
ToolResult, and StateDiff events with no actor attribution.
"""

from __future__ import annotations

import plank  # noqa: F401  registers plank's tool registry
from ensemble import World


def test_apply_emits_tool_call_result_and_diff_with_no_actor():
    world = World("plank", backend="mock")
    out = world.apply(
        "open_ticket",
        ticket_id="t-apply-1",
        user_id="u-alice",
        subject="seeded by world.apply",
    )
    assert out["effect"]["ok"] is True
    assert "diff" in out and out["diff"][0]["table"] == "tickets"

    trace = world.trace()
    calls = [e for e in trace if e["payload"]["kind"] == "tool_call"]
    results = [e for e in trace if e["payload"]["kind"] == "tool_result"]
    diffs = [e for e in trace if e["payload"]["kind"] == "state_diff"]
    assert len(calls) == 1 and calls[0]["payload"]["name"] == "open_ticket"
    assert len(results) == 1 and not results[0]["payload"]["is_error"]
    assert len(diffs) == 1
    for event in calls + results + diffs:
        assert event["actor"] is None, (
            "world.apply records no actor attribution; got "
            f"actor={event['actor']!r}"
        )


def test_apply_propagates_tool_errors_as_is_error_envelope():
    world = World("plank", backend="mock")
    world.apply(
        "issue_refund",
        user_id="u-bob",
        amount_cents=100,
        reason="first",
    )
    out = world.apply(
        "issue_refund",
        user_id="u-bob",
        amount_cents=100,
        reason="second",
    )
    assert out["is_error"] is True
    assert "double refunds" in out["effect"]["error"]


def test_apply_unknown_tool_returns_error_envelope():
    world = World("plank", backend="mock")
    out = world.apply("does_not_exist")
    assert out["is_error"] is True
    assert "unknown tool" in out["effect"]["error"]


def test_apply_then_scheduler_run():
    """world.apply during scenario setup leaves a consistent trace
    that the scheduler then continues to extend. The seed mutation
    stays visible to predicates evaluated after the run."""

    world = World("plank", backend="mock")
    world.apply(
        "open_ticket",
        ticket_id="t-seed",
        user_id="u-alice",
        subject="seed",
    )
    alice = world.spawn_user(id="alice", model="user-model")
    world.spawn_agent(id="rep", model="agent-model", tools=[])
    world._mock_say("agent-model", "ok")
    alice.say("rep", "hi")
    until = (world.turn_count > 1)
    world.run(until)

    trace = world.trace()
    seeded_calls = [
        e for e in trace
        if e["payload"]["kind"] == "tool_call"
        and e["payload"]["name"] == "open_ticket"
        and e["actor"] is None
    ]
    assert len(seeded_calls) == 1
