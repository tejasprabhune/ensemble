"""End-to-end predicate evaluation against a baked agora trace."""

import pytest

import agora  # noqa: F401
from ensemble import scenario
from ensemble.scenario import _REGISTRY


@pytest.mark.asyncio
async def test_hidden_goal_resolved_fires_when_refund_succeeds():
    @scenario("predicates.refund_path")
    async def s(world):
        # Agent issues exactly one refund for alice. Hidden goal resolves.
        world._native._mock_say_then_tool(
            "agent-model",
            "Refunding you now.",
            "issue_refund",
            '{"user_id": "u-alice", "amount_cents": 1500, "reason": "goodwill"}',
        )
        world._mock_say("agent-model", "All set.")
        alice = world.spawn_user(id="alice", model="user-model")
        world.spawn_agent(id="rep", model="agent-model", tools=["issue_refund"])
        alice.act("open_ticket", ticket_id="t-1", user_id="u-alice", subject="x")
        alice.say("rep", "give me the money back")

        yield world.until(world.turn_count > 6)
        yield {
            "alice_goal": 1.0 if alice.hidden_goal_resolved() else 0.0,
            "no_double": 0.0 if world.had_double_refund() else 1.0,
        }

    result = await _REGISTRY["predicates.refund_path"]("agora")
    assert result.scores["alice_goal"] == 1.0
    assert result.scores["no_double"] == 1.0


@pytest.mark.asyncio
async def test_had_double_refund_fires_on_repeat():
    @scenario("predicates.double_refund")
    async def s(world):
        for _ in range(2):
            world._native._mock_say_then_tool(
                "agent-model",
                "Refunding.",
                "issue_refund",
                '{"user_id": "u-alice", "amount_cents": 100, "reason": "x"}',
            )
        world._mock_say("agent-model", "done")
        alice = world.spawn_user(id="alice", model="user-model")
        world.spawn_agent(id="rep", model="agent-model", tools=["issue_refund"])
        alice.say("rep", "refund me twice")
        yield world.until(world.turn_count > 8)
        yield {"double": 1.0 if world.had_double_refund() else 0.0}

    result = await _REGISTRY["predicates.double_refund"]("agora")
    assert result.scores["double"] == 1.0
