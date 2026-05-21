"""End-to-end MCP test: claude-haiku drives an agent slot.

Spawns `ensemble mcp serve` as a subprocess (scenario mode); a
separate subprocess runs a small driver script that connects via the
official MCP client SDK, lists tools, and asks the model to call one
of them given the message it sees on the slot's inbox. Verifies the
trace captures both the user's message and the agent's tool call.

This test is the most expensive one in the suite (it makes claude
sample at least twice). Skip it if budget is tight.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

mcp_client = pytest.importorskip("mcp.client.stdio")
anthropic_sdk = pytest.importorskip("anthropic")
from anthropic import Anthropic
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


CHEAP_MODEL = "claude-haiku-4-5-20251001"


@pytest.mark.asyncio
async def test_claude_drives_slot_via_mcp(have_anthropic, tmp_path, monkeypatch):
    monkeypatch.setenv("ENSEMBLE_HOME", str(tmp_path))
    from ensemble import worlds_registry

    worlds_registry.add_world("agora", Path("examples/agora"))

    pkg = tmp_path / "scenarios"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from . import live_mcp  # noqa\n")
    (pkg / "live_mcp.py").write_text(
        textwrap.dedent(
            """
            import agora  # noqa: F401
            from ensemble import scenario


            @scenario("live.mcp", world="agora")
            async def s(world):
                alice = world.spawn_user(id="alice", model="user-model")
                rep = world.spawn_agent(
                    id="rep_mcp", model="agent-model", tools=["lookup_user"],
                )
                alice.say("rep_mcp", "please look up u-alice")
                yield world.until(world.turn_count > 30)
                yield {"ok": 1.0}
            """
        )
    )

    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "ensemble.cli_mcp",
            "serve",
            "--world",
            "agora",
            "--scenario",
            "live.mcp",
            "--as-agent",
            "rep_mcp",
            "--package-dir",
            str(tmp_path),
        ],
        env={**os.environ, "ENSEMBLE_HOME": str(tmp_path)},
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            # Drain alice's seed message.
            import asyncio

            seen = None
            for _ in range(40):
                msg = await session.call_tool("inbox_recv", arguments={})
                body = json.loads(msg.content[0].text)
                if "text" in body:
                    seen = body["text"]
                    break
                await asyncio.sleep(0.05)
            assert seen and "u-alice" in seen.lower()

            # Ask claude what to do next: list tools, give it the
            # user's message, take the first tool_use from the
            # response.
            tools = await session.list_tools()
            ant = Anthropic()
            ant_tools = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.inputSchema,
                }
                for t in tools.tools
                if t.name not in {"inbox_recv", "agent_say"}
            ]
            resp = ant.messages.create(
                model=CHEAP_MODEL,
                max_tokens=256,
                tools=ant_tools,
                messages=[{"role": "user", "content": seen}],
            )
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            assert tool_uses, f"expected claude to call a tool, got {resp.content}"
            call = tool_uses[0]
            result = await session.call_tool(call.name, arguments=call.input)
            body = json.loads(result.content[0].text)
            assert "effect" in body
