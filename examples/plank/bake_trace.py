"""Bake a deterministic refund_storm trace for the trace viewer demo.

Runs against the mock backend with a hand-authored script. The
agents use the standard tool-use loop, so each turn can both narrate
what it is about to do and call a tool in the same step (matching
real Claude / GPT output). Alice stays frustrated through the policy
work and the refund actually lands at the end of the trace.

Writes the JSONL trace to `site/trace.jsonl`, which the front-page
viewer fetches on load.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import plank  # noqa: F401  registers plank personas with ensemble
from ensemble import RunResult, scenario
from ensemble.scenario import _REGISTRY

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUT = REPO_ROOT / "site" / "trace.jsonl"


@scenario("plank.refund_storm.baked")
async def baked_refund_storm(world):
    # Rep1 plan: acknowledge + lookup, partial offer + policy lookup,
    # escalate, full refund, final confirmation. Each turn is one
    # backend.complete call; with text + tool combined, two turns add
    # up to one user-facing message plus the tool dispatch and result
    # in between, the same shape Claude and GPT produce.
    world._native._mock_say_then_tool(
        "rep1-model",
        "I'm sorry to hear that. Let me pull up your account.",
        "lookup_user",
        json.dumps({"user_id": "u-alice"}),
    )
    world._native._mock_say_then_tool(
        "rep1-model",
        "I see you've been on the team plan since 2022. Policy lets me refund the most recent cycle without review. Let me check what we can do for the full three months.",
        "search_kb",
        json.dumps({"query": "multi-month refund policy"}),
    )
    world._native._mock_say_then_tool(
        "rep1-model",
        "kb-1 confirms multi-month refunds need retention sign-off. I'm escalating the ticket so that team can approve it.",
        "escalate",
        json.dumps({"ticket_id": "t-100", "to_team": "retention"}),
    )
    world._native._mock_say_then_tool(
        "rep1-model",
        "Retention approved the exception. Issuing the full three-month refund now.",
        "issue_refund",
        json.dumps({
            "user_id": "u-alice",
            "amount_cents": 15000,
            "reason": "retention-approved 3 month goodwill refund; tenure 36mo",
        }),
    )
    world._mock_say(
        "rep1-model",
        "All set. $150.00 refunded across the last three billing cycles. The refund will clear in five business days. Anything else I can help with?",
    )

    # Rep2 helps Bob in parallel: KB lookup then explanation. Two
    # turns total, the second has no tool call so the loop exits.
    world._native._mock_say_then_tool(
        "rep2-model",
        "Hi Bob, welcome to Plank. Let me check our docs for you.",
        "search_kb",
        json.dumps({"query": "refund eligibility"}),
    )
    world._mock_say(
        "rep2-model",
        "Refunds can be requested within 14 days of any charge and clear in about five business days. You can do it from Settings > Billing. Want me to walk you through it?",
    )

    # Alice's tone tracks the agent's progress. She pushes back through
    # the partial offer and the policy lookup, then goes quiet while
    # retention runs its review. No premature thank-you.
    alice_lines = [
        "and? i've been waiting on this for weeks already.",
        "one cycle is not the deal. i want all three months back, like i said.",
        "more process. great. how long is this going to take?",
    ]
    for line in alice_lines:
        world._mock_say("alice-model", line)

    # Bob only follows up if rep2 explicitly asks; in this baked run
    # rep2's explanation is conclusive, so bob stays quiet.

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
