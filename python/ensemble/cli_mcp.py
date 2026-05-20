"""Entry point for ``ensemble mcp serve``.

The Rust CLI shells to ``python -m ensemble.cli_mcp serve ...`` so
the MCP server logic stays adjacent to the plugin tools it dispatches.

Phase 4 ships in two steps: this entry point currently exposes the
world's tools as MCP tools (so an external client can list and call
them). Scenario-driving with an external agent slot lands in the
follow-up commit; the ``--scenario`` and ``--as-agent`` flags are
accepted today and produce a clear "not yet wired" message so
downstream tooling can already plumb them.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
import threading
from pathlib import Path
from typing import List, Optional

from .mcp_server import build_scenario_server, build_world_server, serve_stdio
from .world import get_world
from .world_manifest import ManifestError
from .worlds_registry import find_world


def _import_world(name: str) -> None:
    """Import the python package for the named world so it calls
    register_world. Resolves the package path through the worlds
    registry."""
    entry = find_world(name)
    if entry is None:
        print(
            f"error: world {name!r} is not registered; "
            "run `ensemble worlds add <name> <path>` first",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        manifest = entry.manifest()
    except ManifestError as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(2) from e
    if str(entry.path) not in sys.path:
        sys.path.insert(0, str(entry.path))
    importlib.import_module(manifest.python_package)


def cmd_serve(args: argparse.Namespace) -> int:
    _import_world(args.world)
    definition = get_world(args.world)
    if definition is None:
        print(
            f"error: world {args.world!r} did not register itself after import",
            file=sys.stderr,
        )
        return 2

    if args.scenario:
        if not args.as_agent:
            print(
                "error: --scenario requires --as-agent to name which agent "
                "slot the connected client should drive",
                file=sys.stderr,
            )
            return 2
        return _serve_scenario(args, definition)

    server = build_world_server(definition)
    asyncio.run(serve_stdio(server))
    return 0


def _serve_scenario(args: argparse.Namespace, definition) -> int:
    """Run the scenario in a background thread with the named agent
    slot registered as an external proxy. The MCP server takes over
    on stdio; tool calls are attributed to the slot, and the meta
    inbox_recv / agent_say tools plumb scenario messages back and
    forth."""

    if args.package_dir:
        sys.path.insert(0, str(Path(args.package_dir).resolve()))
    # Try to import the scenarios package shipped with the world so
    # the @scenario decorators register their wrappers.
    for module in ("scenarios", f"{definition.name}.scenarios"):
        try:
            importlib.import_module(module)
            break
        except ImportError:
            continue

    from .scenario import _REGISTRY  # noqa: WPS433

    wrapper = _REGISTRY.get(args.scenario)
    if wrapper is None:
        print(
            f"error: no scenario registered as {args.scenario!r}",
            file=sys.stderr,
        )
        return 2

    captured: dict = {"world": None}

    def capture_world(world_obj):
        captured["world"] = world_obj

    scenario_result: dict = {"result": None, "exc": None}

    def run_scenario():
        try:
            scenario_result["result"] = asyncio.run(
                wrapper(
                    definition.name,
                    backend=args.backend,
                    external_agent_id=args.as_agent,
                    on_world_constructed=capture_world,
                )
            )
        except Exception as e:  # noqa: BLE001
            scenario_result["exc"] = e

    thread = threading.Thread(target=run_scenario, daemon=True)
    thread.start()

    # Wait for the scenario to construct its World and spawn the
    # external agent slot. The on_world_constructed callback fires
    # the moment the World is built; we then poll briefly for the
    # spawn that registers the slot itself, since the scenario
    # function may have setup work between World construction and
    # spawn_agent.
    import time

    deadline = time.monotonic() + 5.0
    while captured["world"] is None and time.monotonic() < deadline:
        time.sleep(0.02)

    world_obj = captured["world"]
    if world_obj is not None:
        while (
            world_obj._external_agent is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)

    if world_obj is None or world_obj._external_agent is None:
        scenario_result_exc = scenario_result.get("exc")
        if scenario_result_exc:
            print(
                f"error: scenario raised before registering the agent slot: {scenario_result_exc}",
                file=sys.stderr,
            )
        else:
            print(
                f"error: scenario did not spawn an agent with id {args.as_agent!r} "
                "within 5s; the server has nothing to drive",
                file=sys.stderr,
            )
        return 2

    # Filter to the tools this agent is allowed to see; if the slot
    # was declared with `tools=[]`, fall back to the full world set so
    # the external client at least sees something.
    plugin_tools, _preds = definition.build()
    allowed = set(world_obj._external_agent_tools)
    if allowed:
        plugin_tools = [t for t in plugin_tools if t.name in allowed]
    server = build_scenario_server(
        definition.name, world_obj, args.as_agent, plugin_tools
    )

    asyncio.run(serve_stdio(server))
    thread.join(timeout=30)
    if scenario_result["exc"]:
        print(
            f"warning: scenario raised: {scenario_result['exc']}",
            file=sys.stderr,
        )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ensemble.cli_mcp",
        description="Run an MCP server that exposes an ensemble world's tools.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="Serve the world's tools over MCP stdio.")
    serve.add_argument("--world", required=True, help="World name (must be in the registry).")
    serve.add_argument(
        "--scenario",
        default=None,
        help="Scenario to run while the server is up. Requires --as-agent.",
    )
    serve.add_argument(
        "--as-agent",
        default=None,
        dest="as_agent",
        help="Agent slot the connected MCP client drives (its tool calls and "
        "outbound messages are attributed to this id in the trace).",
    )
    serve.add_argument(
        "--package-dir",
        default=None,
        dest="package_dir",
        help="Directory holding the scenarios package to import. Defaults to "
        "the world's directory.",
    )
    serve.add_argument(
        "--backend",
        default="mock",
        help="LLM backend for the (non-external) actors in the scenario.",
    )
    serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
