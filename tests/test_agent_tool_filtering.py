"""Per-agent tool filtering: spawn_agent(tools=[...]) restricts the dispatch surface."""

from __future__ import annotations

import plank  # noqa: F401  registers the world
import pytest
from ensemble import scenario
from ensemble.scenario import _REGISTRY


@pytest.mark.asyncio
async def test_agent_with_restricted_tools_blocks_excluded_call():
    """An agent declared with tools=['lookup_user'] should see only that
    tool's schema and a hallucinated call to issue_refund should land
    in the trace as an is_error tool result, never reach the registry.
    """

    @scenario("filter.refund_blocked", world="plank")
    async def s(world):
        world._native._mock_say_then_tool(
            "agent-model",
            "Refunding you.",
            "issue_refund",
            '{"user_id": "u-alice", "amount_cents": 500, "reason": "test"}',
        )
        world._mock_say("agent-model", "ok, blocked.")
        alice = world.spawn_user(id="alice", model="user-model")
        world.spawn_agent(
            id="rep",
            model="agent-model",
            tools=["lookup_user"],
        )
        alice.say("rep", "refund me please")
        yield world.until(world.turn_count > 6)
        yield {"ok": 1.0}

    result = await _REGISTRY["filter.refund_blocked"]("plank")
    tool_results = [
        e for e in result.trace if e["payload"]["kind"] == "tool_result"
    ]
    blocked = [
        t for t in tool_results
        if t["payload"]["name"] == "issue_refund" and t["payload"]["is_error"]
    ]
    assert len(blocked) == 1
    assert "not in this agent's allowed set" in blocked[0]["payload"]["result"]["error"]

    refunds = [
        e for e in result.trace
        if e["payload"]["kind"] == "tool_result"
        and e["payload"]["name"] == "issue_refund"
        and not e["payload"]["is_error"]
    ]
    assert refunds == [], "no refund should have actually landed"


@pytest.mark.asyncio
async def test_agent_with_empty_tool_list_blocks_all_dispatches():
    """tools=[] is the bare-NPC case: no tool calls accepted."""

    @scenario("filter.empty_list_blocks_all", world="plank")
    async def s(world):
        world._native._mock_say_then_tool(
            "agent-model",
            "Looking that up.",
            "lookup_user",
            '{"user_id": "u-alice"}',
        )
        world._mock_say("agent-model", "ok.")
        alice = world.spawn_user(id="alice", model="user-model")
        world.spawn_agent(id="rep", model="agent-model", tools=[])
        alice.say("rep", "who am i?")
        yield world.until(world.turn_count > 6)
        yield {"ok": 1.0}

    result = await _REGISTRY["filter.empty_list_blocks_all"]("plank")
    blocked = [
        t for t in result.trace
        if t["payload"]["kind"] == "tool_result"
        and t["payload"]["name"] == "lookup_user"
        and t["payload"]["is_error"]
    ]
    assert len(blocked) == 1


@pytest.mark.asyncio
async def test_agent_with_no_tools_arg_sees_full_registry():
    """When the caller passes no tools kwarg the agent keeps the
    unrestricted default, so the worked-example scenarios that omit
    the field continue to work."""

    @scenario("filter.unrestricted_default", world="plank")
    async def s(world):
        world._native._mock_say_then_tool(
            "agent-model",
            "Looking up alice.",
            "lookup_user",
            '{"user_id": "u-alice"}',
        )
        world._mock_say("agent-model", "ok.")
        alice = world.spawn_user(id="alice", model="user-model")
        world.spawn_agent(id="rep", model="agent-model")
        alice.say("rep", "who am i?")
        yield world.until(world.turn_count > 6)
        yield {"ok": 1.0}

    result = await _REGISTRY["filter.unrestricted_default"]("plank")
    successes = [
        t for t in result.trace
        if t["payload"]["kind"] == "tool_result"
        and t["payload"]["name"] == "lookup_user"
        and not t["payload"]["is_error"]
    ]
    assert len(successes) == 1
    assert successes[0]["payload"]["result"]["data"]["name"] == "Alice Chen"
