"""Sandbox tool worker.

Invoked as ``python -m ensemble.tool_worker --world <name> --tool <name>``
by the parent process when a PluginTool with ``sandbox=True`` is
dispatched. Reads the JSON args from stdin, imports the world's
python package (which re-registers all of its tools and rebuilds a
fresh per-instance state), looks up the requested tool by name, and
prints the tool's JSON response on the final stdout line.

State the parent process held in closures is *not* shared with the
worker. The worker constructs its own state from scratch, runs the
tool once, and exits. The parent's :attr:`World.shared_state` dict
is the one sanctioned cross-boundary channel: the parent serialises
it into the ``ENSEMBLE_SHARED_STATE`` environment variable and the
worker reads it back into a process-global the world's setup can
consult.

World resolution is deterministic. The parent forwards
``ENSEMBLE_SANDBOX_PACKAGE`` (the importable package name) and
``ENSEMBLE_SANDBOX_PACKAGE_DIR`` (the directory containing it) so
the worker imports the exact world the parent loaded. The legacy
fallback (the worlds registry at ``~/.ensemble/worlds.toml``) is
still tried for backward compatibility with workers spawned by
older parents.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path

from .world import get_world


def _bootstrap_shared_state() -> None:
    """Expose the parent's shared_state at module level so the world
    setup can read it. Worlds opt in by reading
    ``ensemble.tool_worker.SHARED_STATE`` in their setup callback;
    setup that does not need it ignores the global."""
    raw = os.environ.get("ENSEMBLE_SHARED_STATE")
    try:
        globals()["SHARED_STATE"] = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        globals()["SHARED_STATE"] = {}


def _import_world_package(world_name: str) -> None:
    """Try the new explicit-package envs first, then fall back to
    the legacy patterns."""
    package = os.environ.get("ENSEMBLE_SANDBOX_PACKAGE")
    pkg_dir = os.environ.get("ENSEMBLE_SANDBOX_PACKAGE_DIR")
    if pkg_dir:
        pkg_dir_path = Path(pkg_dir).expanduser().resolve()
        if str(pkg_dir_path) not in sys.path:
            sys.path.insert(0, str(pkg_dir_path))
    if package:
        importlib.import_module(package)
        return
    # Legacy: assume the world's package shares its name and is
    # already importable from sys.path.
    importlib.import_module(world_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ensemble.tool_worker")
    parser.add_argument("--world", required=True)
    parser.add_argument("--tool", required=True)
    args = parser.parse_args(argv)

    _bootstrap_shared_state()

    definition = get_world(args.world)
    if definition is None and args.world != "noop":
        try:
            _import_world_package(args.world)
        except Exception as e:
            print(
                f"sandbox worker: import of world {args.world!r} failed: {e}",
                file=sys.stderr,
            )
            return 2
        definition = get_world(args.world)

    if definition is None:
        by_name: dict = {}
    else:
        tools, _ = definition.build()
        by_name = {t.name: t for t in tools}
    if args.tool not in by_name:
        print(
            f"sandbox worker: tool {args.tool!r} not registered by world "
            f"{args.world!r}; registered tools: {sorted(by_name)}. "
            f"If the world's package lives outside ~/.ensemble/worlds.toml, "
            f"set ENSEMBLE_SANDBOX_PACKAGE / ENSEMBLE_SANDBOX_PACKAGE_DIR "
            f"in the parent's environment, or pass python_package=... and "
            f"package_dir=... to register_world.",
            file=sys.stderr,
        )
        return 3

    args_json = sys.stdin.read()
    try:
        out = by_name[args.tool].fn(args_json)
    except BaseException as e:
        print(json.dumps({
            "effect": {
                "ok": False,
                "tool": args.tool,
                "summary": f"sandbox worker raised: {type(e).__name__}: {e}",
            }
        }))
        return 4
    try:
        json.loads(out)
    except (TypeError, ValueError) as e:
        print(json.dumps({
            "effect": {
                "ok": False,
                "tool": args.tool,
                "summary": f"sandbox worker: tool returned non-JSON: {e}",
            }
        }))
        return 4
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
