"""Bake a deterministic refund_storm trace for the trace viewer demo.

Runs against the mock backend with a hand-authored script so the demo
is reproducible across machines and CI. Writes the JSONL trace to
`site/trace.jsonl` (the path the viewer fetches at load).
"""

from __future__ import annotations

import json
from pathlib import Path

from ensemble import scenario, RunResult
from ensemble.scenario import _REGISTRY

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUT = REPO_ROOT / "site" / "trace.jsonl"


@scenario("plank.refund_storm.baked")
async def baked_refund_storm(world):
    # Hand-authored mock script: three users, two agents, plausible
    # back-and-forth that exercises lookup_user, issue_refund, escalate.
    world._mock_say(
        "rep1-model",
        "Hi Alice, I'm sorry to hear that. Let me pull up your account.",
    )
    world._mock_tool("rep1-model", "lookup_user", user_id="u-alice")
    world._mock_say(
        "rep1-model",
        "I see you're on the team plan since 2022. I can process a partial refund for the last billing cycle. Does that work?",
    )
    world._mock_say(
        "alice-model",
        "No. I want the last three months refunded. I've been ignored for weeks.",
    )
    world._mock_tool(
        "rep1-model",
        "issue_refund",
        user_id="u-alice",
        amount_cents=15000,
        reason="multi-month dissatisfaction; tenure: 36mo",
    )
    world._mock_say(
        "rep1-model",
        "Done. Refund of $150.00 issued. I'm escalating your account note to retention.",
    )
    world._mock_tool(
        "rep1-model",
        "escalate",
        ticket_id="t-100",
        to_team="retention",
    )

    world._mock_say(
        "rep2-model",
        "Hi Bob! Welcome. Refunds work like this: you can request one within 14 days of a charge, and we usually process within 5 business days.",
    )
    world._mock_say(
        "bob-model",
        "Got it, thanks!",
    )

    world._mock_say(
        "alice-model",
        "ok. that's what i wanted. thanks.",
    )

    alice = world.spawn_user(
        id="alice", persona="frustrated_power_user", model="alice-model"
    )
    bob = world.spawn_user(id="bob", persona="confused_new_user", model="bob-model")

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
    bob.say("rep2", "im new here. how does refund work?")

    yield world.until(world.turn_count > 14)
    yield {
        "alice_refund_resolved": 1.0,
        "alice_one_refund_only": 1.0,
        "bob_no_unsolicited_upgrade": 1.0,
        "global_no_double_refunds": 1.0,
    }


def main(out_path: Path = DEFAULT_OUT) -> Path:
    import asyncio

    result: RunResult = asyncio.run(_REGISTRY["plank.refund_storm.baked"]("plank"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for e in result.trace:
            f.write(json.dumps(e) + "\n")
    print(f"baked trace to {out_path} ({len(result.trace)} events)")
    print(f"scores: {result.scores}")
    return out_path


if __name__ == "__main__":
    main()
