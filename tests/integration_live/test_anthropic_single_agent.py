"""Single-agent plank scenario against the real Anthropic API.

Cheapest claude model + a short prompt; expect a few hundred tokens
total. Verifies the runtime correctly extracts tool calls from
Anthropic's content-block response and routes them back through the
trace.
"""

from __future__ import annotations

import asyncio

import plank  # noqa: F401  registers the world
import pytest
from ensemble import scenario
from ensemble.scenario import _REGISTRY


CHEAP_MODEL = "claude-haiku-4-5-20251001"


@pytest.mark.asyncio
async def test_anthropic_runs_a_lookup(have_anthropic):
    @scenario("live.anthropic_single", world="plank")
    async def s(world):
        rep = world.spawn_agent(
            id="rep",
            model=CHEAP_MODEL,
            tools=["lookup_user"],
            system_prompt=(
                "You are a support rep. The user will mention a user id "
                "starting with 'u-'. Call lookup_user once with that id, "
                "then reply with the user's name in one short sentence. "
                "Do not call any other tools."
            ),
        )
        alice = world.spawn_user(id="alice", model="user-model")
        alice.say("rep", "Quick check, can you look up u-alice for me?")
        yield world.until(world.turn_count > 12)
        yield {"ok": 1.0}

    result = await _REGISTRY["live.anthropic_single"]("plank", backend="anthropic")
    tool_calls = [
        e for e in result.trace if e["payload"]["kind"] == "tool_call"
    ]
    assert any(
        tc["payload"]["name"] == "lookup_user"
        and tc["payload"]["args"].get("user_id") == "u-alice"
        for tc in tool_calls
    ), f"expected lookup_user(u-alice), got {tool_calls}"

    cost_events = [
        e for e in result.trace if e["payload"]["kind"] == "cost"
    ]
    tokens_in = [c for c in cost_events if c["payload"]["unit"] == "tokens_in"]
    tokens_out = [c for c in cost_events if c["payload"]["unit"] == "tokens_out"]
    assert tokens_in, "Anthropic backend should record at least one tokens_in cost"
    assert tokens_out, "Anthropic backend should record at least one tokens_out cost"
    assert all(c["payload"]["amount"] > 0 for c in tokens_in)
    assert all(c["payload"]["amount"] > 0 for c in tokens_out)
    usd = [c for c in cost_events if c["payload"]["unit"] == "usd"]
    assert usd, f"expected usd cost from pricing table for model {CHEAP_MODEL!r}"
    assert all(c["payload"]["amount"] > 0 for c in usd)
