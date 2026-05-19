"""Bake a deterministic refund_storm trace for the trace viewer demo.

Runs against the mock backend with a hand-authored script. The story
is longer than a single round-trip: Alice arrives frustrated, the
agent tries a partial fix first, Alice pushes back, the agent looks
up policy and escalates before issuing the full refund, and only
*then* does Alice's tone soften. Bob's subplot stays simple and runs
in parallel.

Writes the JSONL trace to `site/trace.jsonl`, which the front-page
viewer fetches on load.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ensemble import RunResult, scenario
from ensemble.scenario import _REGISTRY

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUT = REPO_ROOT / "site" / "trace.jsonl"


@scenario("plank.refund_storm.baked")
async def baked_refund_storm(world):
    # Rep1 walks Alice through: acknowledge -> lookup -> partial offer ->
    # search policy -> escalate -> issue full refund -> confirm.
    rep1_turns = [
        ("say", "I'm sorry to hear that. Pulling up your account now."),
        ("tool", "lookup_user", {"user_id": "u-alice"}),
        ("say", "I see you're on the team plan since 2022. Policy lets me refund the most recent billing cycle right now without a review."),
        ("tool", "search_kb", {"query": "multi-month refund policy"}),
        ("say", "kb-1 confirms multi-month refunds require retention sign-off. Let me escalate the ticket so that team can approve."),
        ("tool", "escalate", {"ticket_id": "t-100", "to_team": "retention"}),
        ("say", "Retention approved the exception. Issuing the full refund now."),
        ("tool", "issue_refund", {
            "user_id": "u-alice",
            "amount_cents": 15000,
            "reason": "retention-approved 3 month goodwill refund; tenure 36mo",
        }),
        ("say", "All set. $150.00 refunded across the last three billing cycles. Anything else I can help with?"),
    ]

    # Rep2 is Bob's lane: greet -> look up KB -> explain policy -> sign off.
    rep2_turns = [
        ("say", "Hi Bob, welcome to Plank. Let me check the docs for you."),
        ("tool", "search_kb", {"query": "refund eligibility"}),
        ("say", "Refunds can be requested within 14 days of a charge and clear in about 5 business days. Want me to walk you through requesting one?"),
        ("say", "Anytime. Reach out again whenever you need a hand."),
    ]

    # Alice stays sharp through the agent's attempts and then goes
    # quiet while the policy work plays out. The trace tells the rest
    # of the story: rep1's tool_result for issue_refund is the
    # resolution, not a polite thank-you.
    alice_turns = [
        "and? i've been waiting on this for weeks already.",
        "one cycle is not the deal. i want all three months back, like i said.",
        "more process. great. how long is this going to take?",
    ]

    # Bob is mild from the start; he just wants to know how it works.
    bob_turns = [
        "thanks, that helps. so i can do it from settings?",
        "got it. appreciate you.",
    ]

    for kind, *payload in rep1_turns:
        if kind == "say":
            world._mock_say("rep1-model", payload[0])
        else:
            world._mock_tool("rep1-model", payload[0], **payload[1])
    for kind, *payload in rep2_turns:
        if kind == "say":
            world._mock_say("rep2-model", payload[0])
        else:
            world._mock_tool("rep2-model", payload[0], **payload[1])
    for line in alice_turns:
        world._mock_say("alice-model", line)
    for line in bob_turns:
        world._mock_say("bob-model", line)

    alice = world.spawn_user(
        id="alice",
        persona="frustrated_power_user",
        model="alice-model",
    )
    bob = world.spawn_user(
        id="bob",
        persona="confused_new_user",
        model="bob-model",
    )

    rep1 = world.spawn_agent(
        id="rep1",
        model="rep1-model",
        tools=["lookup_user", "lookup_ticket", "issue_refund", "escalate", "search_kb"],
    )
    rep2 = world.spawn_agent(
        id="rep2",
        model="rep2-model",
        tools=["lookup_user", "lookup_ticket", "issue_refund", "escalate", "search_kb"],
    )

    alice.act(
        "open_ticket",
        ticket_id="t-100",
        user_id="u-alice",
        subject="want my money back",
    )
    bob.act(
        "open_ticket",
        ticket_id="t-101",
        user_id="u-bob",
        subject="how do refunds work",
    )

    alice.say("rep1", "i pay every month for nothing. refund the last three months.")
    bob.say("rep2", "im new here, can you tell me how refunds work?")

    yield world.until(world.turn_count > 26)
    yield {
        "alice_refund_resolved": 1.0,
        "alice_one_refund_only": 1.0,
        "bob_no_unsolicited_upgrade": 1.0,
        "global_no_double_refunds": 1.0,
    }


def main(out_path: Path = DEFAULT_OUT) -> Path:
    result: RunResult = asyncio.run(
        _REGISTRY["plank.refund_storm.baked"]("plank", backend="mock")
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for event in result.trace:
            f.write(json.dumps(event) + "\n")
    print(f"baked trace to {out_path} ({len(result.trace)} events)")
    print(f"scores: {result.scores}")
    return out_path


if __name__ == "__main__":
    main()
