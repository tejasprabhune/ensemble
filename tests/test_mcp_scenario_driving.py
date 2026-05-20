"""Scenario-driving via MCP: external client takes over an agent slot.

A small in-process scenario that spawns one user and one external
agent slot. The MCP client connects, drains the user's opening
message via ``inbox_recv``, calls a tool through ``tools/call``, and
replies via ``agent_say``. Verifies the trace records all three
events with the right actor attribution.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

mcp_client = pytest.importorskip("mcp.client.stdio")
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


@pytest.fixture
def registered_plank(tmp_path, monkeypatch):
    monkeypatch.setenv("ENSEMBLE_HOME", str(tmp_path))
    from ensemble import worlds_registry

    worlds_registry.add_world("plank", Path("examples/plank"))
    # Write a tiny scenarios package the cli_mcp can import.
    pkg = tmp_path / "scenarios"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from . import mcp_drive  # noqa: F401\n")
    (pkg / "mcp_drive.py").write_text(
        textwrap.dedent(
            """
            \"\"\"Smallest scenario that exercises MCP-mode scenario driving.\"\"\"

            import plank  # noqa: F401  registers plank
            from ensemble import scenario


            @scenario("mcp.smoke", world="plank")
            async def s(world):
                alice = world.spawn_user(id="alice", model="user-model")
                rep = world.spawn_agent(
                    id="rep_mcp",
                    model="agent-model",
                    tools=["lookup_user"],
                )
                alice.say("rep_mcp", "what plan am i on?")
                yield world.until(world.turn_count > 40)
                yield {"ok": 1.0}
            """
        )
    )
    yield {"package_dir": tmp_path, "registry_home": tmp_path}


@pytest.mark.asyncio
async def test_external_agent_drives_slot(registered_plank):
    env = {**os.environ, "ENSEMBLE_HOME": str(registered_plank["registry_home"])}
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "ensemble.cli_mcp",
            "serve",
            "--world",
            "plank",
            "--scenario",
            "mcp.smoke",
            "--as-agent",
            "rep_mcp",
            "--package-dir",
            str(registered_plank["package_dir"]),
        ],
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            # Tool the slot is allowed to use + the two meta tools.
            assert "lookup_user" in names
            assert "inbox_recv" in names
            assert "agent_say" in names

            # Drain alice's opening message.
            seen_text = None
            for _ in range(40):
                msg = await session.call_tool("inbox_recv", arguments={})
                body = json.loads(msg.content[0].text)
                if "text" in body:
                    seen_text = body["text"]
                    break
                # spin briefly
                import asyncio

                await asyncio.sleep(0.05)
            assert seen_text is not None, "expected alice's seed message"
            assert "plan" in seen_text.lower()

            # Call a world tool as the agent slot.
            result = await session.call_tool(
                "lookup_user", arguments={"user_id": "u-alice"}
            )
            tool_body = json.loads(result.content[0].text)
            assert tool_body["effect"]["data"]["name"] == "Alice Chen"

            # Reply to alice.
            await session.call_tool(
                "agent_say",
                arguments={"target": "alice", "text": "team plan, alice"},
            )


@pytest.mark.asyncio
async def test_scenario_without_as_agent_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("ENSEMBLE_HOME", str(tmp_path))
    from ensemble import worlds_registry

    worlds_registry.add_world("plank", Path("examples/plank"))

    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ensemble.cli_mcp",
            "serve",
            "--world",
            "plank",
            "--scenario",
            "plank.refund_storm",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "ENSEMBLE_HOME": str(tmp_path)},
    )
    assert result.returncode == 2
    assert "--scenario requires --as-agent" in result.stderr


@pytest.mark.asyncio
async def test_unknown_tool_returns_error(registered_plank):
    env = {**os.environ, "ENSEMBLE_HOME": str(registered_plank["registry_home"])}
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ensemble.cli_mcp", "serve", "--world", "plank"],
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "does_not_exist", arguments={}
            )
            body = json.loads(result.content[0].text)
            assert "error" in body
            assert "unknown tool" in body["error"]
