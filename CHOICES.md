# Choices

Decisions made under ambiguity while building Ensemble.

## Python build & layout

- The repo is a `uv` workspace. The root project (`ensemble`) is a
  maturin-built mixed Rust+Python package; `train/` and `examples/plank`
  are pure-python uv workspace members.
- Maturin's `manifest-path` points at `python/ensemble/Cargo.toml`,
  which is the only crate that produces a `cdylib`. The other crates
  are pure Rust.
- `tool.maturin.features` enables `pyo3/extension-module` only at wheel
  build time so `cargo check`/`cargo test` against the workspace work
  out of the box without needing libpython linkage.

## pyo3 + asyncio bridging

- pyo3 is pinned to 0.21 to stay compatible with `pyo3-asyncio-0-21`,
  the only published asyncio bridge for that line. When the official
  pyo3-asyncio releases for 0.22 lands we'll move both.
- Even with the bridge available, the MVP runs the Rust scheduler on
  a process-global multi-thread tokio runtime and uses synchronous
  `block_on` from pyo3 callbacks. The Python harness exposes
  `world.run(until)` (sync) and `await run.wait_until(condition)`
  (asyncio.to_thread around a blocking native call). This keeps the
  Python call site small and avoids a dependency on the
  python-awaitable bridge for v0.

## Scheduler stop conditions

- The scheduler halts on three conditions: until-predicate fires,
  `max_ticks`/`max_events` exhausted, or quiescence (no new events
  within `quiescence_ms`). Quiescence was added because conversations
  often dry up before the turn-count threshold, which would otherwise
  deadlock the watcher. Default budget is generous; the python
  `start_scheduler` path stretches it further for the `async with`
  pattern.

## World plug-in registration

- For the MVP the python extension bundles Plank directly: the python
  binding crate depends on the `plank` crate and registers `"plank"`
  with the world registry at module init. A future iteration can move
  world authorship into separate pyo3 extension modules that register
  themselves on import; the registry already supports that.

## Trace viewer state-diff source

- The trace viewer's "state changes" panel reads from `state_diff`
  events when they exist, and falls back to summarising `tool_result`
  payloads otherwise. Plank's tools dispatch through `ToolRegistry`
  (which emits `tool_result`), not through `WorldState::apply` (which
  emits `state_diff`). World authors who want richer diffs can wire
  their tools through `world.apply_and_log(bus, actor, name, call)`.

## Grader expressions

- The TOML grader expressions are evaluated by a hand-rolled recursive
  descent parser over `and`/`or`/`not`/parens/names. Names look up into
  a context dict; calls and attribute access are rejected. Avoids
  `eval`/`ast.literal_eval` entirely.

## Training pipeline

- `ensemble-train` is a separate uv package because its torch/transformers
  deps are heavy. Importing the package without the `torch` extra still
  works for spec loading and self-play; only the trainer module raises
  if it is invoked without the deps.
- Modal backend falls back to a dry-run that writes a `DRY_RUN.json`
  when `modal` isn't installed, so the pipeline is at least introspectable
  on a clean machine.

## What we did not build

(Mirrors the spec's "out of scope" list; recorded here so it's visible
without re-reading the prompt.) No hosted platform, no activation
steering, no persona-consistency evaluator (placeholder page only),
no second world, no second trainer, no streaming, no real LLM calls
in tests.
