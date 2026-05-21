# Sweeps

A sweep is the same scenario run across a cartesian product of
configurations: backends, models, seeds, personas, or anything
else expressible as a CLI flag or an environment variable. The
runner produces one trace per cell, captures scores and costs in
a per-cell `meta.json`, and appends a row per cell to a flat
index the cross-run observability subcommands read.

Reach for this page when you want to declare a matrix of runs in
TOML, run it with one command, and resume after an interruption
without re-paying for the completed cells.

## The TOML schema

A sweep is a single TOML file. The minimal shape is one scenario
plus one axis:

```toml
[sweep]
scenario = "my_world.refund"

[sweep.flags]
backend = ["mock"]
```

The full schema:

```toml
[sweep]
scenario = "my_world.refund"
world = "my_world"               # optional; falls through to the scenario's declaration
max_parallel = 4                 # default 1
traces_dir = "traces/refund_sweep"   # default: traces/<scenario_slug>_sweep
package_dir = "."                # optional; path containing the scenarios package

[sweep.flags]
# Each key becomes a --<key> CLI flag passed to ensemble run.
# Each value list contributes one axis to the cartesian product.
backend = ["mock", "anthropic"]

[sweep.env]
# Each key is exported as an env var for the scenario subprocess.
# Use this when the dimension you want to sweep is not a CLI flag.
PLANK_SEED = ["1", "2", "3"]
MY_TEMPERATURE = ["0.0", "0.7"]
```

The two axis tables are independent: `[sweep.flags]` values become
CLI flags on the cell, `[sweep.env]` values become environment
variables. The runner takes the cartesian product across both.

If both tables are empty, the sweep contains exactly one cell
with no overrides.

## Cell identifiers and output layout

Each cell's axis values combine into a stable, filesystem-safe id
that becomes the cell's directory name. With the sweep above and
the axes `backend = ["mock", "anthropic"]` x `PLANK_SEED = ["1",
"2"]`, the runner produces:

```
traces/refund_sweep/
  index.jsonl
  backend-anthropic__PLANK_SEED-1/
    trace.jsonl
    meta.json
  backend-anthropic__PLANK_SEED-2/
    trace.jsonl
    meta.json
  backend-mock__PLANK_SEED-1/
    trace.jsonl
    meta.json
  backend-mock__PLANK_SEED-2/
    trace.jsonl
    meta.json
```

`index.jsonl` is the flat per-cell summary the observability
subcommands read: one JSON line per cell containing the scenario,
the cell's axis values, the scores, the costs, the exit code, and
the trace path.

`meta.json` carries the same data the index row does, plus an
optional `error` field with the last 2000 bytes of the cell's
stderr when the subprocess failed.

## Running, resuming, and parallelism

```sh
ensemble sweep run sweep.toml
```

The runner expands the cartesian product, spawns subprocesses up
to `max_parallel` concurrently, and prints one line per cell to
stderr as cells complete:

```
[ok] backend-mock__PLANK_SEED-1  scores: refund_issued=1.0
[fail] backend-anthropic__PLANK_SEED-2  scores: <none>
[skipped] backend-mock__PLANK_SEED-2  scores: refund_issued=1.0
```

`max_parallel = 1` (the default) serialises cells; higher values
run cells concurrently. Pick the limit based on your backend's
rate budget and on whether your scenario uses external resources
that do not tolerate concurrency.

Resume is the default. A cell whose `meta.json` already exists is
skipped, so a sweep interrupted halfway through can be re-run
without re-paying for the completed cells. Pass `--no-resume` to
force every cell to re-run.

The summary line on stdout (after all cells complete) is one JSON
object with the total cell count, the failed count, the traces
directory, and the index path:

```json
{"scenario":"my_world.refund","cells":6,"failed":0,"traces_dir":"traces/refund_sweep","index":"traces/refund_sweep/index.jsonl"}
```

The exit code is 0 when every cell succeeded and 1 when any cell
failed.

## Reading sweep results

The sweep's `index.jsonl` is a per-cell flat file. The
cross-run observability subcommands (see
[observability.md](observability.md)) can be pointed at the
sweep's directory:

```sh
ensemble runs list --traces-dir traces/refund_sweep
ensemble runs export --traces-dir traces/refund_sweep --format csv > results.csv
```

`ensemble runs compare` takes two cell ids (or unique prefixes) and
diffs their scores side by side. `ensemble trace compare` opens the
two cells' traces in the browser viewer, scroll-synced by tick.

## When the cartesian product is wrong

If two axes should move together rather than independently, the
sweep schema does not support a zipped product directly. The
workaround is one cell per combination encoded as a single axis
value:

```toml
[sweep.flags]
preset = ["fast", "balanced", "thorough"]
```

Then the scenario reads the preset name from the CLI flag or env
var and picks the matching settings internally. This trades schema
simplicity for the loss of axis-level reporting; an explicit zip
operator may land later if the audit's next round finds it
load-bearing.
