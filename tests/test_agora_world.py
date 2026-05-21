"""Agora world boots, tools are registered, agents can dispatch them."""

import pytest

from ensemble import scenario
from ensemble.scenario import _REGISTRY


@pytest.mark.asyncio
async def test_agora_agent_dispatches_lookup_user():
    @scenario("agora_lookup")
    async def agora_lookup(world):
        # Script the agent to issue a lookup_user tool call, then say ok.
        world._mock_tool("agent-model", "lookup_user", user_id="u-alice")
        world._mock_say("agent-model", "alice is on the team plan")
        world._mock_say("user-model", "great, thanks")

        alice = world.spawn_user(id="alice", model="user-model")
        rep = world.spawn_agent(id="rep", model="agent-model", tools=["lookup_user"])
        alice.say("rep", "can you look me up?")

        yield world.until(world.turn_count > 4)
        yield {"ok": 1.0}

    # Construct against the agora world this time.
    from ensemble.scenario import World
    w = World("agora")
    # Re-run the scenario manually against the agora world; the
    # decorator defaults to noop, so use the underlying function.
    result = await _REGISTRY["agora_lookup"]("agora")
    tool_calls = [e for e in result.trace if e["payload"]["kind"] == "tool_call"]
    tool_results = [e for e in result.trace if e["payload"]["kind"] == "tool_result"]
    assert any(tc["payload"]["name"] == "lookup_user" for tc in tool_calls)
    assert any(
        tr["payload"]["name"] == "lookup_user"
        and tr["payload"]["result"]["data"]["name"] == "Alice Chen"
        for tr in tool_results
    )
