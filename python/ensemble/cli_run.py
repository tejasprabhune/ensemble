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

import time as _time

from .worlds_registry import find_world
from .world_manifest import ManifestError, load_manifest as _load_world_manifest


def _append_runs_index(traces_dir: Path, row: dict) -> None:
    """Append a single-line JSON record to traces/runs.jsonl. The
    cross-run subcommands read this file."""
    index = traces_dir / "runs.jsonl"
    with index.open("a") as f:
        f.write(json.dumps(row) + "\n")


def _autodiscover_cwd_world() -> Optional[tuple[str, Path]]:
    """If the cwd contains a ``world.toml``, parse it, add the cwd to
    sys.path, and import the declared python package so
    ``register_world`` fires. Returns ``(world_name, cwd)`` so the
    caller can plug them in as defaults; returns ``None`` when no
    manifest is found or the manifest cannot be loaded.

    Lets ``ensemble run my_world.smoke`` work straight after
    ``ensemble init my_world && cd my_world`` without the explicit
    ``ensemble worlds add`` step the audit called out."""
    cwd = Path.cwd()
    manifest_path = cwd / "world.toml"
    if not manifest_path.exists():
        return None
    try:
        manifest = _load_world_manifest(manifest_path)
    except ManifestError as e:
        print(f"warning: ignoring world.toml in cwd: {e}", file=sys.stderr)
        return None
    _add_package_dir(cwd)
    try:
        importlib.import_module(manifest.python_package)
    except ImportError as e:
        print(
            f"warning: world.toml in cwd names python_package "
            f"{manifest.python_package!r} but importing it failed: {e}",
            file=sys.stderr,
        )
        return None
    print(
        f"ensemble: auto-registered world {manifest.name!r} from ./world.toml",
        file=sys.stderr,
    )
    return (manifest.name, cwd)


def _add_package_dir(p: Path) -> None:
    p = p.resolve()
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _import_scenarios_package(package_dir: Optional[Path]) -> None:
    """Make the scenarios in the supplied directory visible to the
    @scenario decorator's global registry. The directory is expected
    to contain a ``scenarios/`` subpackage with one module per
    scenario.

    Two-step import. First try the conventional package import so any
    ``__init__.py`` side effects fire (plank lists its scenarios
    there). Then walk the directory and import any module the
    ``__init__`` did not list, so dropping a new ``scenarios/foo.py``
    is enough to register a new scenario without editing the
    ``__init__``."""
    if package_dir is None:
        return
    _add_package_dir(package_dir)
    scenarios_root = package_dir.resolve() / "scenarios"
    try:
        importlib.import_module("scenarios")
    except ImportError as primary:
        if not scenarios_root.is_dir():
            print(
                f"warning: could not import scenarios package from {package_dir}: {primary}",
                file=sys.stderr,
            )
            return

    if not scenarios_root.is_dir():
        return

    import importlib.util as _util  # noqa: WPS433  (local import keeps the cli startup cheap)

    for module_path in sorted(scenarios_root.glob("*.py")):
        if module_path.name.startswith("_"):
            continue
        mod_name = f"scenarios.{module_path.stem}"
        if mod_name in sys.modules:
            # The package's __init__ already imported it; do not
            # double-execute the module body.
            continue
        spec = _util.spec_from_file_location(mod_name, module_path)
        if spec is None or spec.loader is None:
            continue
        module = _util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            del sys.modules[mod_name]
            print(
                f"warning: failed to import {module_path}: {e}",
                file=sys.stderr,
            )


def _resolve_world(name: Optional[str]) -> Optional[Path]:
    """Look the world up in ~/.ensemble/worlds.toml and add its python
    package dir to sys.path so importing it triggers register_world.
    Returns the world's directory so the caller can default package_dir
    to the scenarios that ship with the world."""
    if not name or name == "noop":
        return None
    entry = find_world(name)
    if entry is None:
        return None
    try:
        manifest = entry.manifest()
    except ManifestError as e:
        print(f"warning: world {name!r} manifest is invalid: {e}", file=sys.stderr)
        return entry.path
    # The world's python package usually lives at <root>/<python_package>;
    # the parent of that dir goes on sys.path so `import <python_package>`
    # resolves. Importing the package runs register_world.
    _add_package_dir(entry.path)
    try:
        importlib.import_module(manifest.python_package)
    except ImportError as e:
        print(
            f"warning: importing world package {manifest.python_package!r} from {entry.path}: {e}",
            file=sys.stderr,
        )
    return entry.path


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ensemble.cli_run",
        description="Run a registered ensemble scenario and write its trace.",
    )
    parser.add_argument("--scenario", required=True, help="Registered scenario name.")
    parser.add_argument(
        "--world",
        default=None,
        help="Name of the world to construct. Resolves through "
        "~/.ensemble/worlds.toml; defaults to whatever the scenario "
        "declared on @scenario(..., world=...).",
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

    world_name = args.world
    package_dir: Optional[Path] = args.package_dir

    # Auto-discover a world.toml in the cwd when the caller did not
    # pass --package-dir. This is what makes
    # `ensemble init my_world && cd my_world && ensemble run my_world.smoke`
    # work in one step.
    if package_dir is None:
        auto = _autodiscover_cwd_world()
        if auto is not None:
            auto_name, auto_dir = auto
            if world_name is None:
                world_name = auto_name
            package_dir = auto_dir

    # Registry lookup: needed when the world lives outside the cwd or
    # when the scenario was started with an explicit --world that does
    # not match the cwd's manifest. Skip when auto-discovery already
    # imported the package, since _resolve_world would try to import
    # it a second time.
    if world_name and (package_dir is None or world_name != getattr(args, "world", world_name)):
        world_root = _resolve_world(world_name)
        if package_dir is None:
            package_dir = world_root

    # Final fallback: the README quickstart runs from the repo root with
    # no flags; examples/plank is the bundled world there.
    if package_dir is None and Path("examples/plank/world.toml").is_file():
        package_dir = Path("examples/plank")
        if world_name is None:
            world_name = "plank"

    _import_scenarios_package(package_dir)

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

    args.traces_dir.mkdir(parents=True, exist_ok=True)
    safe = args.scenario.replace("/", "_").replace(".", "_")
    flat_trace_path = args.traces_dir / f"{safe}.jsonl"

    # Each run writes traces/<run_id>/ holding trace.jsonl and meta.json.
    # The run_id is a UUID7 generated by the World constructor. We
    # capture it via on_world_constructed so the trace directory is
    # named after the same ID the World uses internally.
    run_state: dict = {}

    def _on_world_constructed(world_obj) -> None:
        run_id = world_obj.run_id
        run_dir = args.traces_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        trace_path = run_dir / "trace.jsonl"
        world_obj.set_trace_path(str(trace_path))
        print(f"Run id: {run_id}", file=sys.stderr)
        sweep_id = os.environ.get("ENSEMBLE_STAGE_SWEEP_ID")
        run_url = world_obj.init_stage_run(args.scenario, sweep_id=sweep_id)
        if run_url:
            print(f"Stage:  {run_url}", file=sys.stderr)
        run_state["run_id"] = run_id
        run_state["run_dir"] = run_dir
        run_state["trace_path"] = trace_path

    started_ts = _time.time()
    result = asyncio.run(
        _REGISTRY[args.scenario](
            world_name,
            backend=args.backend,
            on_world_constructed=_on_world_constructed,
        )
    )
    finished_ts = _time.time()

    run_id = run_state["run_id"]
    run_dir = run_state["run_dir"]
    trace_path = run_state["trace_path"]

    meta = {
        "run_id": run_id,
        "scenario": args.scenario,
        "world": world_name,
        "backend": args.backend,
        "started_at": started_ts,
        "finished_at": finished_ts,
        "duration_s": finished_ts - started_ts,
        "scores": result.scores,
        "costs": result.costs,
        "trace_path": str(trace_path),
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    _append_runs_index(args.traces_dir, meta)

    # Keep the flat path as a symlink to the latest run so the
    # README quickstart and the trace viewer's flat-path examples
    # keep working without per-user surgery.
    try:
        if flat_trace_path.exists() or flat_trace_path.is_symlink():
            flat_trace_path.unlink()
        flat_trace_path.symlink_to(trace_path.relative_to(args.traces_dir))
    except OSError:
        # Symlinks may fail on filesystems that disallow them. Fall
        # back to a plain copy so the flat path still resolves.
        import shutil
        shutil.copyfile(trace_path, flat_trace_path)

    summary: dict = {
        "scenario": args.scenario,
        "run_id": run_id,
        "scores": result.scores,
        "trace_path": str(trace_path),
    }
    if result.costs:
        summary["costs"] = result.costs
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
