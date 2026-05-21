# Cross-run observability

Once each `ensemble run` lands in its own directory and a row is
appended to `traces/runs.jsonl`, cross-run questions are cheap:
list recent runs, dump one run's meta, diff two runs' scores side
by side, open two traces in the viewer scroll-synced by tick, or
export the index as JSON or CSV for downstream analysis.

This page covers the subcommands and the data layout they read.

## On-disk layout

Every `ensemble run` produces:

```
traces/
  runs.jsonl
  20260520T143022_my_world_smoke_a1b2c3d4/
    trace.jsonl
    meta.json
  my_world_smoke.jsonl -> 20260520T143022_my_world_smoke_a1b2c3d4/trace.jsonl
```

The run id is a sortable timestamp plus an 8-hex disambiguator so
`ls traces/` shows the newest run last and tab completion still
distinguishes runs of the same scenario at the same second.

The flat `traces/<scenario>.jsonl` symlink points at the latest
run so the README quickstart and any tooling that hard-codes the
flat path keep working.

`runs.jsonl` is the append-only index. One row per completed run:

```json
{
  "run_id": "20260520T143022_my_world_smoke_a1b2c3d4",
  "scenario": "my_world.smoke",
  "world": "my_world",
  "backend": "mock",
  "started_at": 1779326173.89,
  "finished_at": 1779326175.90,
  "duration_s": 2.01,
  "scores": {"ok": 1.0},
  "costs": {},
  "trace_path": "traces/20260520T143022_my_world_smoke_a1b2c3d4/trace.jsonl"
}
```

Per-run `meta.json` carries the same fields.

## ensemble runs list

```sh
ensemble runs list [--scenario NAME] [--limit N]
```

Prints recent runs as a table sorted oldest-first:

```
run_id                                                   scenario          when                 scores
-----------------------------------------------------------------------------------------------------------
20260520T143022_my_world_smoke_a1b2c3d4                  my_world.smoke    2026-05-20 14:30:22  ok=1.0
20260520T144210_my_world_smoke_b2c3d4e5                  my_world.smoke    2026-05-20 14:42:10  ok=0.5
```

`--scenario` filters by scenario name. `--limit N` shows only the
last N rows. `--traces-dir` (global) points at a directory other
than `./traces`; pass the sweep's directory to list a sweep's
cells.

## ensemble runs show

```sh
ensemble runs show <run_id_or_prefix>
```

Prints one run's meta as pretty-printed JSON. The argument is the
full run id or any unique prefix; the command resolves ambiguity
by reporting all matches and exiting non-zero so you can disambiguate
with a longer prefix.

## ensemble runs compare

```sh
ensemble runs compare <a_id_or_prefix> <b_id_or_prefix>
```

Diffs two runs' scores side by side. The output names each metric
once, with the A value, the B value, and the signed delta:

```
A: 20260520T143022_my_world_smoke_a1b2c3d4  scenario=my_world.smoke  when=2026-05-20 14:30:22
B: 20260520T144210_my_world_smoke_b2c3d4e5  scenario=my_world.smoke  when=2026-05-20 14:42:10

metric                                       A               B  delta
--------------------------------------------------------------------------------
ok                                          1.0             0.5      -0.500
speed                                       0.8             0.9      +0.100

costs:
  A: {"tokens_in": 4500, "usd": 0.013}
  B: {"tokens_in": 4800, "usd": 0.014}
```

Pair with `ensemble trace compare` to open the two traces side by
side in the browser viewer.

## ensemble runs export

```sh
ensemble runs export [--format json|csv]
```

Emits the full index. `--format json` is the default and writes a
JSON array of meta records. `--format csv` flattens scores into
one column per metric so the result loads into pandas without
re-parsing JSON:

```
run_id,scenario,world,backend,finished_at,duration_s,score.ok,score.speed
20260520T143022_my_world_smoke_a1b2c3d4,my_world.smoke,my_world,mock,1779326175.9,2.01,1.0,0.8
20260520T144210_my_world_smoke_b2c3d4e5,my_world.smoke,my_world,mock,1779327730.1,2.05,0.5,0.9
```

## ensemble trace compare

```sh
ensemble trace compare <trace_a.jsonl> <trace_b.jsonl> [--port 8765]
```

Serves a two-column browser view of the two traces. Each column
renders one trace's message events (agent and user messages, tool
calls, tool results, system notes) as a chronological feed. The
sync-scroll toggle (on by default) ties the columns by tick so
equivalent moments in both runs sit at the same vertical
position.

The compare assets ship embedded in the binary so the command
works offline without `--site`. `--site <dir>` is still honoured
for live editing of the HTML and JS during development.

## Filtering and ad-hoc analysis

The index format is intentionally flat JSONL so a researcher can
read it directly in any language. A common pattern:

```python
import json
from pathlib import Path

rows = [
    json.loads(line)
    for line in Path("traces/runs.jsonl").read_text().splitlines()
    if line.strip()
]
recent = [r for r in rows if r["scenario"] == "my_world.refund"][-20:]
print(sum(r["scores"]["refund_issued"] for r in recent) / len(recent))
```

For sweep results the same pattern works against the sweep's
`index.jsonl`; the cells are rows in the same shape.
