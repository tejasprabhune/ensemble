"""Entry point used by `ensemble sweep run <config.toml>`.

The sweep TOML declares a scenario and a cartesian product of CLI
flags and environment variables. The runner expands the product,
runs one scenario invocation per cell (in parallel up to
max_parallel), writes one trace per cell, and appends a row to an
index file the observability subcommands read.

Minimum config (one axis, one value):

    [sweep]
    scenario = "plank.refund_storm"

    [sweep.flags]
    backend = ["mock"]

Typical config (cartesian product):

    [sweep]
    scenario = "plank.refund_storm"
    world = "plank"
    max_parallel = 4
    traces_dir = "traces/refund_sweep"

    [sweep.flags]
    backend = ["mock", "auto"]

    [sweep.env]
    PLANK_SEED = ["1", "2", "3"]

Resume semantics: cells whose meta.json already exists are skipped
unless --no-resume is passed.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if sys.version_info >= (3, 11):
    import tomllib as _toml
else:
    import tomli as _toml  # type: ignore[import-not-found]


@dataclass
class SweepConfig:
    scenario: str
    world: Optional[str]
    max_parallel: int
    traces_dir: Path
    package_dir: Optional[Path]
    flags: Dict[str, List[str]]
    env: Dict[str, List[str]]
    raw: Dict[str, Any]


def _load_sweep(path: Path) -> SweepConfig:
    data = _toml.loads(path.read_text())
    sweep = data.get("sweep")
    if not isinstance(sweep, dict):
        raise ValueError(f"{path}: missing required [sweep] table")
    scenario = sweep.get("scenario")
    if not scenario:
        raise ValueError(f"{path}: [sweep].scenario is required")
    world = sweep.get("world")
    max_parallel = int(sweep.get("max_parallel", 1))
    if max_parallel < 1:
        raise ValueError(f"{path}: max_parallel must be >= 1")
    traces_dir = Path(sweep.get("traces_dir", f"traces/{scenario.replace('.', '_')}_sweep"))
    package_dir = Path(sweep["package_dir"]) if "package_dir" in sweep else None

    raw_flags = sweep.get("flags", {})
    flags: Dict[str, List[str]] = {}
    if not isinstance(raw_flags, dict):
        raise ValueError(f"{path}: [sweep.flags] must be a table")
    for k, v in raw_flags.items():
        if not isinstance(v, list) or not v:
            raise ValueError(f"{path}: [sweep.flags].{k} must be a non-empty list")
        flags[k] = [str(x) for x in v]

    raw_env = sweep.get("env", {})
    env: Dict[str, List[str]] = {}
    if not isinstance(raw_env, dict):
        raise ValueError(f"{path}: [sweep.env] must be a table")
    for k, v in raw_env.items():
        if not isinstance(v, list) or not v:
            raise ValueError(f"{path}: [sweep.env].{k} must be a non-empty list")
        env[k] = [str(x) for x in v]

    return SweepConfig(
        scenario=str(scenario),
        world=str(world) if world else None,
        max_parallel=max_parallel,
        traces_dir=traces_dir,
        package_dir=package_dir,
        flags=flags,
        env=env,
        raw=sweep,
    )


def _expand_cells(cfg: SweepConfig) -> List[Tuple[Dict[str, str], Dict[str, str]]]:
    """Cartesian product of flags x env. Each cell is a pair
    (flag_assignment, env_assignment). Empty axes collapse to a
    single cell, so a sweep that only varies one dimension does not
    produce a sea of duplicates."""
    flag_keys = sorted(cfg.flags.keys())
    env_keys = sorted(cfg.env.keys())
    flag_values = [cfg.flags[k] for k in flag_keys] or [[None]]
    env_values = [cfg.env[k] for k in env_keys] or [[None]]
    cells: List[Tuple[Dict[str, str], Dict[str, str]]] = []
    for flag_combo in itertools.product(*flag_values):
        flag_assignment = {
            k: v for k, v in zip(flag_keys, flag_combo) if v is not None
        }
        for env_combo in itertools.product(*env_values):
            env_assignment = {
                k: v for k, v in zip(env_keys, env_combo) if v is not None
            }
            cells.append((flag_assignment, env_assignment))
    return cells


def _cell_id(flags: Dict[str, str], env: Dict[str, str]) -> str:
    """A stable, filesystem-safe id from the cell's axis values. Used
    as the cell's directory name so a researcher can find a specific
    cell later by name."""
    parts: List[str] = []
    for k in sorted(flags):
        parts.append(f"{k}-{flags[k]}")
    for k in sorted(env):
        parts.append(f"{k}-{env[k]}")
    if not parts:
        return "cell"
    return "__".join(parts).replace("/", "_").replace(" ", "_")


async def _run_cell(
    cfg: SweepConfig,
    flags: Dict[str, str],
    env: Dict[str, str],
    cell_root: Path,
    sem: asyncio.Semaphore,
) -> Dict[str, Any]:
    """Spawn one scenario invocation as a subprocess. Returns the
    parsed JSON summary the cli_run entry point prints, plus the
    cell's axis values."""
    async with sem:
        cell_root.mkdir(parents=True, exist_ok=True)
        trace_path = cell_root / "trace.jsonl"

        argv = [
            sys.executable,
            "-m",
            "ensemble.cli_run",
            "--scenario",
            cfg.scenario,
            "--traces-dir",
            str(cell_root),
        ]
        if cfg.world:
            argv.extend(["--world", cfg.world])
        if cfg.package_dir is not None:
            argv.extend(["--package-dir", str(cfg.package_dir)])
        for k, v in flags.items():
            argv.extend([f"--{k}", v])

        child_env = os.environ.copy()
        child_env.update(env)

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
        )
        stdout, stderr = await proc.communicate()
        text = stdout.decode().strip()
        last = text.splitlines()[-1] if text else ""
        try:
            summary = json.loads(last) if last else {}
        except json.JSONDecodeError:
            summary = {}

        meta = {
            "scenario": cfg.scenario,
            "world": cfg.world,
            "flags": flags,
            "env": env,
            "exit_code": proc.returncode,
            "scores": summary.get("scores", {}),
            "costs": summary.get("costs", {}),
            "trace_path": summary.get("trace_path", str(trace_path)),
        }
        if proc.returncode != 0:
            stderr_tail = stderr.decode()[-2000:]
            meta["error"] = f"exit {proc.returncode}: {stderr_tail.strip()}"

        (cell_root / "meta.json").write_text(json.dumps(meta, indent=2))
        return meta


async def _run_sweep(
    cfg: SweepConfig,
    resume: bool,
    on_cell_complete,
) -> List[Dict[str, Any]]:
    cells = _expand_cells(cfg)
    sem = asyncio.Semaphore(cfg.max_parallel)
    tasks = []
    for flags, env in cells:
        cid = _cell_id(flags, env)
        cell_root = cfg.traces_dir / cid
        if resume and (cell_root / "meta.json").exists():
            existing = json.loads((cell_root / "meta.json").read_text())
            on_cell_complete(cid, existing, skipped=True)
            tasks.append(asyncio.sleep(0, result=existing))
            continue

        async def run_one(flags=flags, env=env, cell_root=cell_root, cid=cid):
            meta = await _run_cell(cfg, flags, env, cell_root, sem)
            on_cell_complete(cid, meta, skipped=False)
            return meta

        tasks.append(asyncio.create_task(run_one()))

    return await asyncio.gather(*tasks)


def _write_index(traces_dir: Path, rows: List[Dict[str, Any]]) -> Path:
    """Write a flat newline-delimited JSON index summarising the sweep.
    One row per cell; the observability subcommands read this file."""
    path = traces_dir / "index.jsonl"
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return path


def run(args: argparse.Namespace) -> int:
    cfg = _load_sweep(args.config)
    cfg.traces_dir.mkdir(parents=True, exist_ok=True)

    completed: List[Dict[str, Any]] = []

    def on_cell_complete(cid: str, meta: Dict[str, Any], skipped: bool) -> None:
        status = "skipped" if skipped else ("ok" if meta.get("exit_code") == 0 else "fail")
        scores = meta.get("scores") or {}
        score_summary = ", ".join(f"{k}={v}" for k, v in scores.items()) or "<none>"
        print(f"[{status}] {cid}  scores: {score_summary}", file=sys.stderr)
        completed.append(meta)

    asyncio.run(_run_sweep(cfg, resume=not args.no_resume, on_cell_complete=on_cell_complete))

    index_path = _write_index(cfg.traces_dir, completed)
    summary = {
        "scenario": cfg.scenario,
        "cells": len(completed),
        "failed": sum(1 for m in completed if m.get("exit_code") not in (0, None)),
        "traces_dir": str(cfg.traces_dir),
        "index": str(index_path),
    }
    print(json.dumps(summary))
    return 0 if summary["failed"] == 0 else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ensemble.cli_sweep",
        description="Run a sweep: cartesian product of CLI flags and env vars.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="Run a sweep from a sweep.toml.")
    p_run.add_argument("config", type=Path, help="Path to the sweep TOML.")
    p_run.add_argument(
        "--no-resume",
        action="store_true",
        help="Re-run cells whose meta.json already exists (default: skip them).",
    )
    p_run.set_defaults(func=run)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
