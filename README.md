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

- Rust stable (1.85+; workspace edition 2021, some transitive deps require edition2024)
- Python 3.10+
- `uv` for Python env and package management

## Install

```sh
# Python side: build the extension and install the workspace.
uv sync

# Rust CLI: installs the `ensemble` binary onto ~/.cargo/bin so it
# lands on your PATH. Re-run this command whenever you pull new
# changes; the install replaces whatever was previously on PATH.
cargo install --path crates/ensemble-cli
```

If you prefer not to install to PATH, `cargo build -p ensemble-cli`
puts the binary at `./target/debug/ensemble`; use that path explicitly
in place of bare `ensemble` in every command below.

`uv sync` builds the `ensemble` extension module via maturin, installs
the `plank` example package, and installs the `ensemble-train` training
package. Optional torch extras for training are not installed by default:

```sh
uv pip install 'ensemble-train[torch]'
```

## Run

The complete on-ramp lives on the documentation site at
[tejasprabhune.github.io/ensemble/quickstart.html](https://tejasprabhune.github.io/ensemble/quickstart.html).
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

# Run a matrix of configurations (see the Sweeps page on the docs site).
./target/debug/ensemble sweep run sweep.toml

# Re-bake the deterministic mock trace used on the site.
uv run python examples/plank/bake_trace.py

# Kick off persona training (modal by default).
./target/debug/ensemble train examples/plank/personas/frustrated_power_user.toml \
  --backend modal
```

## Stage

Stage is the optional cloud observability backend. Set two environment
variables and every run, sweep, and training job streams its events to
Stage in parallel with the local trace:

```sh
export ENSEMBLE_STAGE_API_KEY=stage_sk_...
export ENSEMBLE_STAGE_PROJECT=myorg/popcornbench

# Runs now print a Stage URL alongside the run id:
./target/debug/ensemble run plank.refund_storm
# Run id: 019542a3-4e7b-7000-8e1d-3f9a1c2d5e6f
# Stage:  https://stage.ensemble.sh/myorg/popcornbench/runs/019542a3-...

# ensemble runs list merges local and Stage results:
./target/debug/ensemble runs list

# Push older local traces retroactively:
./target/debug/ensemble stage push traces/
```

To set up Stage for the first time:

1. Sign in at [ensemble-stage.fly.dev](https://ensemble-stage.fly.dev) with GitHub.
2. Go to [/me](https://ensemble-stage.fly.dev/me) and create a push-scoped API key. Copy the key when it appears; it is shown exactly once.
3. Create a project at `https://ensemble-stage.fly.dev/your-github-login` using the inline form.
4. Set the two environment variables above and run any scenario.

Stage is entirely optional. Local JSONL traces are always written first
and are complete whether or not Stage is reachable. See
[docs/reference/stage](https://tejasprabhune.github.io/ensemble/reference/stage.html)
for the full configuration reference.

## Test

```sh
cargo test --workspace
uv run pytest tests/
```
