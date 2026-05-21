"""End-to-end test of `ensemble.cli_mcp serve` exposing agora's tools.

Spawns the MCP server as a subprocess with a tmp registry pointing at
the agora example, connects to it via the official MCP client SDK
over stdio, and verifies that tools/list returns agora's tools and
that tools/call routes through to agora's SQLite-backed rust code.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

mcp_client = pytest.importorskip("mcp.client.stdio")
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


@pytest.fixture
def registered_agora(tmp_path, monkeypatch):
    """Register agora under a tmp ENSEMBLE_HOME so the test does not
    pollute the developer's machine."""
    monkeypatch.setenv("ENSEMBLE_HOME", str(tmp_path))
    from ensemble import worlds_registry

    worlds_registry.add_world("agora", Path("examples/agora"))
    yield


@pytest.mark.asyncio
async def test_tools_list_returns_agora_tools(registered_agora):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ensemble.cli_mcp", "serve", "--world", "agora"],
        env={**os.environ},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            for expected in [
                "open_ticket",
                "lookup_user",
                "lookup_ticket",
                "issue_refund",
                "escalate",
                "search_kb",
                "update_subscription",
            ]:
                assert expected in names, f"missing tool {expected!r}"


@pytest.mark.asyncio
async def test_tools_call_routes_to_agora(registered_agora):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ensemble.cli_mcp", "serve", "--world", "agora"],
        env={**os.environ},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "lookup_user", arguments={"user_id": "u-alice"}
            )
            assert result.content, "expected at least one content block"
            body = json.loads(result.content[0].text)
            assert body["effect"]["data"]["name"] == "Alice Chen"


@pytest.mark.asyncio
async def test_tools_call_emits_diff(registered_agora):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ensemble.cli_mcp", "serve", "--world", "agora"],
        env={**os.environ},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "open_ticket",
                arguments={
                    "ticket_id": "t-mcp-1",
                    "user_id": "u-alice",
                    "subject": "from mcp",
                },
            )
            body = json.loads(result.content[0].text)
            assert body["effect"]["ok"] is True
            # State-changing tools emit a diff alongside the effect.
            assert "diff" in body and body["diff"][0]["table"] == "tickets"
