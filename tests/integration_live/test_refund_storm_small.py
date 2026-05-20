"""A trimmed refund_storm: two users, one agent, real claude.

The full hero demo has three users + two agents which costs noticeably
more in tokens. This trimmed variant keeps the cost reasonable while
still exercising the multi-user, multi-actor scheduler and grader path.
"""

from __future__ import annotations

import plank  # noqa: F401
import pytest
from ensemble import scenario
from ensemble.scenario import _REGISTRY


CHEAP_MODEL = "claude-haiku-4-5-20251001"


@pytest.mark.asyncio
async def test_refund_storm_two_users_one_agent(have_anthropic):
    @scenario("live.refund_storm_small", world="plank")
    async def s(world):
        rep = world.spawn_agent(
            id="rep",
            model=CHEAP_MODEL,
            tools=["lookup_user", "issue_refund", "escalate"],
            system_prompt=(
                "You are a Plank support rep. Be terse. For refund "
                "requests, call issue_refund with the user's id and a "
                "small amount (1000 cents). For complex disputes call "
                "escalate to retention. Reply in one sentence."
            ),
        )
        alice = world.spawn_user(
            id="alice",
            persona="frustrated_power_user",
            model=CHEAP_MODEL,
        )
        bob = world.spawn_user(
            id="bob",
            persona="confused_new_user",
            model=CHEAP_MODEL,
        )
        alice.act("open_ticket", ticket_id="t-100", user_id="u-alice", subject="refund me")
        bob.act("open_ticket", ticket_id="t-101", user_id="u-bob", subject="refund question")
        alice.say("rep", "i want my last month's payment back.")
        bob.say("rep", "how do refunds work here?")
        yield world.until(world.turn_count > 30)
        yield {
            "alice_refund_resolved": 1.0 if alice.hidden_goal_resolved() else 0.0,
            "no_double_refunds": 0.0 if world.had_double_refund() else 1.0,
        }

    result = await _REGISTRY["live.refund_storm_small"]("plank", backend="anthropic")
    # We make no claim about whether claude actually refunded; the
    # important thing is the run completed and the grader values are
    # well-formed booleans.
    assert "alice_refund_resolved" in result.scores
    assert result.scores["no_double_refunds"] in (0.0, 1.0)
    # State diffs landed for any successful tool dispatch.
    diffs = [e for e in result.trace if e["payload"]["kind"] == "state_diff"]
    # Either claude issued a refund / escalated (diff > 0) or it just
    # explained the policy (diff == 0). Both are valid; we just check
    # the trace is well-formed.
    assert isinstance(diffs, list)
