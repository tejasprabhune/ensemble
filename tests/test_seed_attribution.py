"""User.act and World.apply mark their events with seed=true.

Runtime tool dispatches (the AgentActor's turn, dispatch_as for an
MCP-driven external slot) leave the seed flag false. The flag rides
along on ToolCall, ToolResult, and StateDiff so a trace consumer can
filter setup mutations from agent-driven decisions without joining
on tool id.
"""

from __future__ import annotations

import plank  # noqa: F401  registers the world
import pytest
from ensemble import World, scenario
from ensemble.scenario import _REGISTRY


def test_user_act_marks_events_as_seed():
    world = World("plank", backend="mock")
    alice = world.spawn_user(id="alice", model="user-model")
    alice.act(
        "open_ticket",
        ticket_id="t-seed-1",
        user_id="u-alice",
        subject="seeded",
    )
    trace = world.trace()
    calls = [e for e in trace if e["payload"]["kind"] == "tool_call"]
    results = [e for e in trace if e["payload"]["kind"] == "tool_result"]
    diffs = [e for e in trace if e["payload"]["kind"] == "state_diff"]
    assert calls and all(c["payload"]["seed"] is True for c in calls)
    assert results and all(r["payload"]["seed"] is True for r in results)
    assert diffs and all(d["payload"]["seed"] is True for d in diffs)


def test_world_apply_marks_events_as_seed():
    world = World("plank", backend="mock")
    world.apply(
        "open_ticket",
        ticket_id="t-apply-2",
        user_id="u-alice",
        subject="applied",
    )
    trace = world.trace()
    for kind in ("tool_call", "tool_result", "state_diff"):
        evs = [e for e in trace if e["payload"]["kind"] == kind]
        assert evs and all(e["payload"]["seed"] is True for e in evs)


@pytest.mark.asyncio
async def test_agent_actor_dispatches_are_not_marked_seed():
    """An AgentActor runtime turn produces unseeded events: the
    agent decided to call the tool, no scenario seed is in play."""

    @scenario("seed.agent_runtime", world="plank")
    async def s(world):
        world._native._mock_say_then_tool(
            "agent-model",
            "looking up.",
            "lookup_user",
            '{"user_id": "u-alice"}',
        )
        world._mock_say("agent-model", "done.")
        alice = world.spawn_user(id="alice", model="user-model")
        world.spawn_agent(id="rep", model="agent-model", tools=["lookup_user"])
        alice.say("rep", "who am i?")
        yield world.until(world.turn_count > 6)
        yield {"ok": 1.0}

    result = await _REGISTRY["seed.agent_runtime"]("plank")
    calls = [
        e for e in result.trace
        if e["payload"]["kind"] == "tool_call"
        and e["payload"]["name"] == "lookup_user"
    ]
    assert calls and all(c["payload"]["seed"] is False for c in calls)
    results = [
        e for e in result.trace
        if e["payload"]["kind"] == "tool_result"
        and e["payload"]["name"] == "lookup_user"
    ]
    assert results and all(r["payload"]["seed"] is False for r in results)
