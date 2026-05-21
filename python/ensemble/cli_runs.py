"""Entry point used by `ensemble runs ...`.

Reads the per-run index at traces/runs.jsonl (and each run's
meta.json) to support cross-run observability: listing, showing,
comparing, and exporting runs without making the researcher walk
the traces directory by hand.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def _load_index(traces_dir: Path) -> List[Dict[str, Any]]:
    path = traces_dir / "runs.jsonl"
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _format_scores(scores: Dict[str, Any]) -> str:
    if not scores:
        return "<none>"
    return ", ".join(f"{k}={v}" for k, v in scores.items())


def _format_iso(ts: Optional[float]) -> str:
    if ts is None:
        return "-"
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def cmd_list(args: argparse.Namespace) -> int:
    rows = _load_index(args.traces_dir)
    if args.scenario:
        rows = [r for r in rows if r.get("scenario") == args.scenario]
    if args.limit is not None:
        rows = rows[-args.limit:]
    if not rows:
        print("no runs", file=sys.stderr)
        return 0

    print(f"{'run_id':<55}  {'scenario':<32}  {'when':<19}  scores")
    print("-" * 130)
    for r in rows:
        print(
            f"{r.get('run_id', '?'):<55}  "
            f"{(r.get('scenario') or '?')[:32]:<32}  "
            f"{_format_iso(r.get('finished_at')):<19}  "
            f"{_format_scores(r.get('scores') or {})}"
        )
    return 0


def _find_run(traces_dir: Path, run_id: str) -> Optional[Dict[str, Any]]:
    """Look the run up in the index, falling back to a prefix match
    on the run id so the user does not have to type the whole
    timestamp+hash string."""
    rows = _load_index(traces_dir)
    exact = [r for r in rows if r.get("run_id") == run_id]
    if exact:
        return exact[0]
    prefix = [r for r in rows if (r.get("run_id") or "").startswith(run_id)]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        print(
            f"ambiguous run id {run_id!r}; matches: "
            + ", ".join(r["run_id"] for r in prefix),
            file=sys.stderr,
        )
        return None
    return None


def cmd_show(args: argparse.Namespace) -> int:
    row = _find_run(args.traces_dir, args.run_id)
    if row is None:
        print(f"no run matches {args.run_id!r}", file=sys.stderr)
        return 2
    print(json.dumps(row, indent=2))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    a = _find_run(args.traces_dir, args.a)
    b = _find_run(args.traces_dir, args.b)
    if a is None or b is None:
        return 2
    print(f"A: {a['run_id']}  scenario={a.get('scenario')}  when={_format_iso(a.get('finished_at'))}")
    print(f"B: {b['run_id']}  scenario={b.get('scenario')}  when={_format_iso(b.get('finished_at'))}")
    print()
    print(f"{'metric':<32}  {'A':>14}  {'B':>14}  delta")
    print("-" * 80)
    keys = sorted(set((a.get("scores") or {}).keys()) | set((b.get("scores") or {}).keys()))
    for k in keys:
        av = (a.get("scores") or {}).get(k)
        bv = (b.get("scores") or {}).get(k)
        try:
            delta = f"{(bv - av):+.3f}" if av is not None and bv is not None else "-"
        except TypeError:
            delta = "-"
        print(f"{k:<32}  {str(av):>14}  {str(bv):>14}  {delta:>10}")
    print()
    print("costs:")
    print(f"  A: {json.dumps(a.get('costs') or {})}")
    print(f"  B: {json.dumps(b.get('costs') or {})}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    rows = _load_index(args.traces_dir)
    if args.format == "json":
        print(json.dumps(rows, indent=2))
        return 0
    if args.format == "csv":
        # Flatten scores into one column per metric so the CSV is
        # easy to load into pandas without re-parsing JSON.
        import csv
        all_score_keys = sorted({
            k for r in rows for k in (r.get("scores") or {})
        })
        writer = csv.writer(sys.stdout)
        writer.writerow(
            ["run_id", "scenario", "world", "backend", "finished_at", "duration_s"]
            + [f"score.{k}" for k in all_score_keys]
        )
        for r in rows:
            scores = r.get("scores") or {}
            writer.writerow(
                [
                    r.get("run_id"),
                    r.get("scenario"),
                    r.get("world"),
                    r.get("backend"),
                    r.get("finished_at"),
                    r.get("duration_s"),
                ]
                + [scores.get(k, "") for k in all_score_keys]
            )
        return 0
    print(f"unknown format {args.format!r}", file=sys.stderr)
    return 2


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ensemble.cli_runs",
        description="Cross-run observability: list, show, compare, export.",
    )
    parser.add_argument(
        "--traces-dir",
        type=Path,
        default=Path("traces"),
        help="Where the runs index lives (default: ./traces).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="Print recent runs as a table.")
    p_list.add_argument("--scenario", help="Filter by scenario name.")
    p_list.add_argument("--limit", type=int, help="Show only the last N rows.")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Print one run's meta as JSON.")
    p_show.add_argument("run_id", help="Run id or unique prefix.")
    p_show.set_defaults(func=cmd_show)

    p_compare = sub.add_parser("compare", help="Diff two runs' scores side by side.")
    p_compare.add_argument("a", help="First run id or unique prefix.")
    p_compare.add_argument("b", help="Second run id or unique prefix.")
    p_compare.set_defaults(func=cmd_compare)

    p_export = sub.add_parser("export", help="Emit the full runs index.")
    p_export.add_argument(
        "--format",
        choices=["json", "csv"],
        default="json",
        help="Output format (default: json).",
    )
    p_export.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
