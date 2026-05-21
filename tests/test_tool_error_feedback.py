"""Tool errors emit ToolResult(is_error=true) and reach the agent's history."""

import pytest

import agora  # noqa: F401
from ensemble import scenario
from ensemble.scenario import _REGISTRY


@pytest.mark.asyncio
async def test_failed_tool_call_lands_as_is_error_result():
    @scenario("errors.double_refund_replies")
    async def s(world):
        # First refund succeeds; the second is blocked by the
        # per-user-per-run policy and lands as a tool error.
        world._native._mock_say_then_tool(
            "agent-model",
            "Refunding the first month.",
            "issue_refund",
            '{"user_id": "u-alice", "amount_cents": 500, "reason": "first"}',
        )
        world._native._mock_say_then_tool(
            "agent-model",
            "Trying the second.",
            "issue_refund",
            '{"user_id": "u-alice", "amount_cents": 500, "reason": "second"}',
        )
        world._mock_say("agent-model", "I saw the error and stopped.")

        alice = world.spawn_user(id="alice", model="user-model")
        world.spawn_agent(id="rep", model="agent-model", tools=["issue_refund"])
        alice.say("rep", "refund me twice please")

        yield world.until(world.turn_count > 10)
        yield {"ok": 1.0}

    result = await _REGISTRY["errors.double_refund_replies"]("agora")
    tool_results = [
        e for e in result.trace if e["payload"]["kind"] == "tool_result"
    ]
    errored = [t for t in tool_results if t["payload"]["is_error"]]
    assert len(errored) == 1, "exactly one of the two refunds should error"
    assert "double refunds" in errored[0]["payload"]["result"]["error"]
