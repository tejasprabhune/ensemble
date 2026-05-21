"""Integration test: two users, one agent, mock backend, end-to-end."""

import pytest

from ensemble import scenario
from ensemble.scenario import _REGISTRY


@pytest.mark.asyncio
async def test_two_users_one_agent_with_tool_dispatch():
    @scenario("integration.basic")
    async def basic(world):
        # Agent does one lookup_user tool call then answers; users
        # then exchange a couple of acknowledgements.
        world._mock_tool("agent-model", "lookup_user", user_id="u-alice")
        world._mock_say("agent-model", "you're on the team plan, alice")
        world._mock_say("user-model", "thanks, that helps")
        world._mock_say("agent-model", "happy to help, anything else?")
        world._mock_say("user-model", "no that's it")

        alice = world.spawn_user(id="alice", persona="frustrated_power_user", model="user-model")
        bob = world.spawn_user(id="bob", persona="confused_new_user", model="user-model")
        rep = world.spawn_agent(id="rep", model="agent-model", tools=["lookup_user"])

        alice.say("rep", "what plan am i on?")
        bob.say("rep", "any updates?")

        yield world.until(world.turn_count > 6)
        yield {"completed": 1.0}

    result = await _REGISTRY["integration.basic"]("agora")

    # Grader output.
    assert result.scores == {"completed": 1.0}

    # Trace correctness: every event is well-formed.
    for e in result.trace:
        assert "tick" in e
        assert "ts_ms" in e
        assert "payload" in e
        assert "kind" in e["payload"]

    # State-diff completeness: agent's lookup_user tool call produced a
    # tool_result. Agora tools don't currently emit a typed Diff through
    # the registry; the StateDiff event is reserved for the
    # WorldState::apply path used by world authors writing in Rust.
    # The tool dispatch path emits a ToolResult, which is what the
    # trace viewer renders as a state change line.
    tool_calls = [e for e in result.trace if e["payload"]["kind"] == "tool_call"]
    tool_results = [e for e in result.trace if e["payload"]["kind"] == "tool_result"]
    assert any(tc["payload"]["name"] == "lookup_user" for tc in tool_calls)
    assert any(
        tr["payload"]["name"] == "lookup_user"
        and tr["payload"]["result"]["ok"] is True
        for tr in tool_results
    )

    # Both users contributed messages.
    user_msgs = [e for e in result.trace if e["payload"]["kind"] == "user_message"]
    speakers = {e["actor"] for e in user_msgs if e.get("actor")}
    assert "alice" in speakers
    assert "bob" in speakers
