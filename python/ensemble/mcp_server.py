"""MCP server that exposes an ensemble world's tools.

External MCP-aware agents (Claude Code, Codex, Claude Desktop, etc.)
connect over stdio, list the world's tools, and call them. When the
server runs in scenario mode (see :func:`build_scenario_server`), the
named agent slot is driven by the connected client: tool calls land
in the world's trace attributed to that agent, and special meta tools
let the client receive messages routed to the slot and send messages
on its behalf.

We use the official Python MCP SDK (``mcp.server.lowlevel.Server``)
rather than the Rust ``rmcp`` crate: ensemble's plugin tools are
python callables and round-tripping every call through pyo3 only to
land in a python callable adds latency without gain. See
:doc:`CHOICES` for the longer-form note.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from .world import PluginPredicate, PluginTool, WorldDefinition


SERVER_VERSION = "0.1.0"


@dataclass
class McpToolOutcome:
    """What a tool dispatch produced. We surface effect + diff to the
    client as a single text block (JSON-encoded) so external agents
    that aren't ensemble-aware still see something useful."""

    effect: Any
    diff: Optional[Any] = None
    is_error: bool = False

    @classmethod
    def from_plugin_result(cls, raw: str) -> "McpToolOutcome":
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            return cls(effect={"error": f"tool returned non-json: {e}"}, is_error=True)
        if isinstance(parsed, dict):
            return cls(effect=parsed.get("effect"), diff=parsed.get("diff"))
        return cls(effect=parsed)

    def to_content(self) -> List[types.TextContent]:
        body: Dict[str, Any] = {"effect": self.effect}
        if self.diff is not None:
            body["diff"] = self.diff
        return [types.TextContent(type="text", text=json.dumps(body, default=str))]


def build_tools_server(
    name: str,
    tools: Sequence[PluginTool],
    *,
    record: Optional[Callable[[str, Dict[str, Any], McpToolOutcome], None]] = None,
) -> Server:
    """Build an MCP server that exposes the supplied plugin tools.

    ``record``, when provided, is called for every successful tool
    dispatch so the caller can mirror it into a trace.
    """
    server: Server = Server(name)
    by_name = {t.name: t for t in tools}

    @server.list_tools()
    async def _list_tools() -> List[types.Tool]:
        return [
            types.Tool(
                name=t.name,
                description=t.description,
                inputSchema=t.parameters,
            )
            for t in tools
        ]

    @server.call_tool()
    async def _call_tool(
        tool_name: str, arguments: Dict[str, Any]
    ) -> List[types.TextContent]:
        tool = by_name.get(tool_name)
        if tool is None:
            return [
                types.TextContent(
                    type="text",
                    text=json.dumps({"error": f"unknown tool {tool_name!r}"}),
                )
            ]
        args_json = json.dumps(arguments or {})
        # Plugin callables are sync; run them in the default executor
        # so we don't block the server's event loop on long-running
        # tool implementations.
        loop = asyncio.get_running_loop()
        try:
            raw = await loop.run_in_executor(None, tool.fn, args_json)
        except Exception as e:  # noqa: BLE001
            outcome = McpToolOutcome(
                effect={"error": str(e)}, is_error=True
            )
            if record is not None:
                record(tool_name, arguments or {}, outcome)
            return outcome.to_content()
        outcome = McpToolOutcome.from_plugin_result(raw)
        if record is not None:
            record(tool_name, arguments or {}, outcome)
        return outcome.to_content()

    return server


def build_world_server(definition: WorldDefinition) -> Server:
    """Spin up an MCP server that mirrors a world's tools and
    predicates. Predicates are exposed as zero-arg tools that return
    a boolean; this is enough for clients that want to introspect a
    run mid-flight."""
    tools, predicates = definition.build()
    plugin_tools: List[PluginTool] = list(tools)
    plugin_tools.extend(_predicates_as_tools(predicates))
    return build_tools_server(definition.name, plugin_tools)


def _predicates_as_tools(predicates: Sequence[PluginPredicate]) -> List[PluginTool]:
    """Wrap each predicate as a zero-arg MCP tool. The trace argument
    is fed empty since this server-only server has no scenario context;
    callers running the predicate against a real trace should call the
    scenario-driving form instead."""
    out: List[PluginTool] = []
    for p in predicates:
        def make_fn(pred: PluginPredicate) -> Callable[[str], str]:
            def fn(_args_json: str) -> str:
                value = pred.fn("[]", "{}")
                return json.dumps({"effect": {"value": bool(value)}})

            return fn

        out.append(
            PluginTool(
                name=f"predicate.{p.name}",
                description=f"Evaluate the {p.name!r} predicate against an empty trace. "
                "Use the scenario-driving server form for evaluation against a live run.",
                parameters={"type": "object", "properties": {}, "required": []},
                fn=make_fn(p),
            )
        )
    return out


async def serve_stdio(server: Server) -> None:
    """Run ``server`` on stdio until the connected client disconnects."""
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=server.name,
                server_version=SERVER_VERSION,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )
