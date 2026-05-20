"""Entry point used by `ensemble run`.

The Rust CLI shells out to ``python -m ensemble.cli_run`` rather than
embedding an inline ``-c`` script, so errors land in tracebacks instead
of opaque string-formatting failures and the flag surface is real
``argparse`` rather than positional concatenation.

Usage from the CLI side:

    python -m ensemble.cli_run \
        --scenario plank.refund_storm \
        --world plank \
        --package-dir examples/plank

The output line is a single JSON object with the chosen scenario name,
the grader scores, and the path the trace was written to.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
from pathlib import Path
from typing import List, Optional


def _add_package_dir(p: Path) -> None:
    p = p.resolve()
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _import_scenarios_package(package_dir: Optional[Path]) -> None:
    """Make the scenarios in the supplied directory visible to the
    @scenario decorator's global registry. The directory is expected to
    contain either a `scenarios/` subpackage or a flat set of scenario
    modules under a `scenarios` name."""
    if package_dir is None:
        return
    _add_package_dir(package_dir)
    try:
        importlib.import_module("scenarios")
    except ImportError as e:
        # Surface the underlying import error so a misnamed module is
        # not confused with the package being absent.
        print(
            f"warning: could not import scenarios package from {package_dir}: {e}",
            file=sys.stderr,
        )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ensemble.cli_run",
        description="Run a registered ensemble scenario and write its trace.",
    )
    parser.add_argument("--scenario", required=True, help="Registered scenario name.")
    parser.add_argument(
        "--world",
        default="plank",
        help="Name of the world to construct (default: plank).",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Optional scenarios.toml manifest to load before running.",
    )
    parser.add_argument(
        "--package-dir",
        type=Path,
        help="Directory holding a `scenarios/` python package to import.",
    )
    parser.add_argument(
        "--backend",
        default=None,
        help="Override the LLM backend (mock | anthropic | openai | vllm | auto).",
    )
    parser.add_argument(
        "--traces-dir",
        type=Path,
        default=Path("traces"),
        help="Where to write the trace JSONL (default: ./traces).",
    )
    args = parser.parse_args(argv)

    _import_scenarios_package(args.package_dir)

    # Imported here so the manifest-derived scenarios share the same
    # registry as the package-imported scenarios.
    from ensemble import load_manifest  # noqa: WPS433
    from ensemble.scenario import _REGISTRY  # noqa: WPS433

    if args.manifest is not None:
        load_manifest(args.manifest)

    if args.scenario not in _REGISTRY:
        registered = ", ".join(sorted(_REGISTRY)) or "<none>"
        print(
            f"unknown scenario {args.scenario!r}; registered: {registered}",
            file=sys.stderr,
        )
        return 2

    result = asyncio.run(
        _REGISTRY[args.scenario](args.world, backend=args.backend)
    )

    args.traces_dir.mkdir(parents=True, exist_ok=True)
    safe = args.scenario.replace("/", "_").replace(".", "_")
    trace_path = args.traces_dir / f"{safe}.jsonl"
    with trace_path.open("w") as f:
        for event in result.trace:
            f.write(json.dumps(event) + "\n")

    print(
        json.dumps(
            {
                "scenario": args.scenario,
                "scores": result.scores,
                "trace_path": str(trace_path),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
