"""Single-agent plank scenario against the real OpenAI API.

Cheapest model + short prompt. Verifies the runtime correctly
extracts tool calls from OpenAI's `function_calling` shape and
routes them to the world's tool registry.
"""

from __future__ import annotations

import plank  # noqa: F401
import pytest
from ensemble import scenario
from ensemble.scenario import _REGISTRY


CHEAP_MODEL = "gpt-4o-mini"


@pytest.mark.asyncio
async def test_openai_runs_a_lookup(have_openai):
    @scenario("live.openai_single", world="plank")
    async def s(world):
        rep = world.spawn_agent(
            id="rep",
            model=CHEAP_MODEL,
            tools=["lookup_user"],
            system_prompt=(
                "You are a support rep. The user will mention a user id "
                "starting with 'u-'. Call lookup_user once with that id, "
                "then reply with the user's name in one short sentence."
            ),
        )
        alice = world.spawn_user(id="alice", model="user-model")
        alice.say("rep", "Please look up u-alice and tell me their name.")
        yield world.until(world.turn_count > 12)
        yield {"ok": 1.0}

    result = await _REGISTRY["live.openai_single"]("plank", backend="openai")
    tool_calls = [
        e for e in result.trace if e["payload"]["kind"] == "tool_call"
    ]
    assert any(
        tc["payload"]["name"] == "lookup_user"
        and tc["payload"]["args"].get("user_id") == "u-alice"
        for tc in tool_calls
    )
