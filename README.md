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

- Rust 1.80+ (workspace edition 2021)
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

```sh
# Run a registered scenario; writes the trace to ./traces/.
./target/debug/ensemble run plank.refund_storm --world plank

# Re-bake the deterministic mock trace used on the site.
uv run python examples/plank/bake_trace.py

# Serve the site locally with the baked trace.
./target/debug/ensemble trace view site/trace.jsonl --site site --port 8765

# Scaffold a new world.
./target/debug/ensemble init my_world

# Kick off persona training (modal by default).
./target/debug/ensemble train examples/plank/personas/frustrated_power_user.toml \
  --backend modal
```

## Test

```sh
cargo test --workspace
uv run pytest tests/
```
