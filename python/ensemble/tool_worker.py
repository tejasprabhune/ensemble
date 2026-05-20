"""Sandbox tool worker.

Invoked as ``python -m ensemble.tool_worker --world <name> --tool <name>``
by the parent process when a PluginTool with ``sandbox=True`` is
dispatched. Reads the JSON args from stdin, imports the world's
python package (which re-registers all of its tools and rebuilds a
fresh per-instance state), looks up the requested tool by name, and
prints the tool's JSON response on the final stdout line.

State the parent process held in closures is *not* shared with the
worker. The worker constructs its own state from scratch, runs the
tool once, and exits. This is the contract: sandbox=True tools have
to fit their work inside the args they receive.

Exit codes:

* 0 - tool returned a JSON envelope on stdout
* 1 - bad CLI usage
* 2 - failed to import the world package
* 3 - tool not found
* 4 - tool raised an exception that the worker did not turn into a
      JSON-envelope error
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys

from .world import get_world


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ensemble.tool_worker")
    parser.add_argument("--world", required=True)
    parser.add_argument("--tool", required=True)
    args = parser.parse_args(argv)

    definition = get_world(args.world)
    if definition is None and args.world != "noop":
        # The world has not been imported yet in this fresh
        # interpreter. Try the conventional name: most worlds ship a
        # python package whose import side-effect-registers them.
        try:
            importlib.import_module(args.world)
        except Exception as e:
            print(f"sandbox worker: import of world {args.world!r} failed: {e}", file=sys.stderr)
            return 2
        definition = get_world(args.world)

    if definition is None:
        # "noop" is the built-in placeholder world; it has no plugin
        # tools. We still let the worker run so callers get a clean
        # "tool not registered" error rather than a missing-world one.
        by_name: dict = {}
    else:
        tools, _ = definition.build()
        by_name = {t.name: t for t in tools}
    if args.tool not in by_name:
        print(
            f"sandbox worker: tool {args.tool!r} not registered by world "
            f"{args.world!r}; registered tools: {sorted(by_name)}",
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
    # Guard against a tool that forgot to return a JSON envelope.
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
