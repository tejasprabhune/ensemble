# Ensemble

A private monorepo for building multi-user, multi-agent RL environments.
Ensemble provides the core simulation framework (Rust), a Python SDK for
authoring scenarios, a CLI, and a post-training pipeline. Plank is the
worked example world, demonstrating the framework in a small SaaS
customer-support setting.

## Layout

```
crates/
  ensemble-core/      simulation primitives (Rust)
  ensemble-runtime/   LLM client layer, tool runtime
  ensemble-cli/       the `ensemble` binary
python/ensemble/      pyo3 bindings + scenario decorators
train/                ensemble_train: persona post-training
examples/plank/       worked example world (Rust + Python + personas)
site/                 static multi-page site, no framework
tests/                python integration tests
```

## Requirements

- Rust 1.85+ (workspace edition 2021; some transitive deps require edition2024, stabilized in 1.85)
- Python 3.10+
- `uv` for Python env and package management

## Install

```sh
# Python side: build the extension and install the workspace.
uv sync

# Rust CLI: builds the `ensemble` binary into ./target/debug/ensemble.
cargo build -p ensemble-cli
```

`uv sync` builds the `ensemble` extension module via maturin, installs
the `plank` example package, and installs the `ensemble-train` training
package. Optional torch extras for training are not installed by default:

```sh
uv pip install 'ensemble-train[torch]'
```

## Run

The complete on-ramp lives at [docs/quickstart.md](docs/quickstart.md).
A short selection of common commands:

```sh
# Scaffold a new pure-Python world and run its smoke scenario.
./target/debug/ensemble init my_world
cd my_world
./target/debug/ensemble run my_world.smoke

# Run a registered scenario from the repo root; writes per-run dirs to ./traces/.
./target/debug/ensemble run plank.refund_storm --world plank

# See what backends and models are available, plus key status.
./target/debug/ensemble models list

# Inspect cross-run history.
./target/debug/ensemble runs list
./target/debug/ensemble runs compare <run_id_a> <run_id_b>

# Open one trace in the browser, or compare two side by side.
./target/debug/ensemble trace view traces/plank_refund_storm.jsonl
./target/debug/ensemble trace compare traces/<run_a>/trace.jsonl traces/<run_b>/trace.jsonl

# Run a matrix of configurations (see docs/reference/sweeps.md).
./target/debug/ensemble sweep run sweep.toml

# Re-bake the deterministic mock trace used on the site.
uv run python examples/plank/bake_trace.py

# Kick off persona training (modal by default).
./target/debug/ensemble train examples/plank/personas/frustrated_power_user.toml \
  --backend modal
```

## Test

```sh
cargo test --workspace
uv run pytest tests/
```
