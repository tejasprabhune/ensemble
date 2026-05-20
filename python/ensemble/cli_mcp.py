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
from pathlib import Path
from typing import List, Optional

from .mcp_server import build_world_server, serve_stdio
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

    if args.scenario or args.as_agent:
        # The scenario-driving path will land in a follow-up commit;
        # for now, surface a clear message so callers don't think it
        # is silently working.
        print(
            "note: --scenario and --as-agent are recognised but not yet "
            "wired; the server exposes the world's tools only. "
            "(Phase 4 follow-up.)",
            file=sys.stderr,
        )

    server = build_world_server(definition)
    asyncio.run(serve_stdio(server))
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
        help="Scenario to run while the server is up. Phase 4 follow-up.",
    )
    serve.add_argument(
        "--as-agent",
        default=None,
        dest="as_agent",
        help="Agent slot the connected client takes over. Phase 4 follow-up.",
    )
    serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
