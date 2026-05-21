# Quickstart

This page walks from "nothing installed" to "I have a scenario
running and I understand what just happened" in about ten minutes.
It assumes you write Python, you have used a CLI before, and you
want to evaluate or train an LLM agent in a multi-actor setting.

## Install

Ensemble is a Rust workspace plus a Python extension. You need
both toolchains once, then everything runs from a single binary.

```sh
# from the repo root
uv sync
cargo build -p ensemble-cli
```

`uv sync` builds the Python extension via maturin and installs the
workspace. `cargo build -p ensemble-cli` produces the `ensemble`
binary at `./target/debug/ensemble`. Put it on your `PATH` or use
the full path in the commands below.

## First run

The default scaffold is a single-file Python world. Two commands
take you from nothing to a working trace:

```sh
ensemble init my_world
cd my_world
ensemble run my_world.smoke
```

The first command lays down a small project: a `world.toml`
manifest, a `my_world.py` with one example tool, a
`scenarios/smoke.py`, and a README. Nothing in this scaffold
requires Rust; researchers who only want to write Python never
touch the heavyweight path.

The second command discovers the `world.toml` in the cwd,
registers the world automatically, runs the smoke scenario
against the deterministic mock backend, and writes the trace into
`./traces/<run_id>/`. Stdout prints a single JSON summary line
with the scenario, the run id, the scores, and the trace path.

Because no API key was set, the run uses the mock backend and you
see a loud bracketed warning telling you so. Mock replies are
canned and deterministic. Read the next section to wire up a real
backend.

## Pointing at a real model

Set a key in your shell or in a `.env` next to the scenario, then
ask for a real backend:

```sh
export ANTHROPIC_API_KEY=sk-ant-...
ensemble run my_world.smoke --backend auto
```

`--backend auto` picks the first backend whose key is set
(Anthropic first, then OpenAI). You can also name the backend
explicitly: `--backend anthropic`, `--backend openai`,
`--backend vllm`. The chosen backend is announced on stderr before
the first LLM call so a silent fall-through is impossible.

To see what models each backend accepts, plus whether your keys
are currently set:

```sh
ensemble models list
```

The model id you pass to `world.spawn_agent(model="...")` (or
declare in the world's `default_agent_model`) must match one of
the listed identifiers for the relevant backend.

## Looking at the trace

Each run lands in a dedicated directory:

```
traces/
  runs.jsonl
  20260520T143022_my_world_smoke_a1b2c3d4/
    trace.jsonl
    meta.json
  my_world_smoke.jsonl -> 20260520T143022_my_world_smoke_a1b2c3d4/trace.jsonl
```

`runs.jsonl` is a flat append-only index across all runs. The
per-run directory holds the trace plus a meta file with scenario,
backend, scores, costs, and durations. The flat symlink keeps the
README quickstart short and tools that point at the canonical path
working.

To see recent runs as a table:

```sh
ensemble runs list
```

To inspect one run by id (prefix matching, so you only type the
distinguishing part):

```sh
ensemble runs show 20260520
```

To diff two runs' scores side by side:

```sh
ensemble runs compare 20260520T1430 20260520T1442
```

To open a single trace in the browser viewer:

```sh
ensemble trace view traces/my_world_smoke.jsonl
```

To compare two traces side by side, scroll-synced by tick:

```sh
ensemble trace compare \
  traces/20260520T1430_.../trace.jsonl \
  traces/20260520T1442_.../trace.jsonl
```

## Adding a real tool

Open `my_world.py` from the scaffold. The starter is an `echo`
tool that returns its input. Replace it with whatever your world
needs. The decorator infers the name, the description, and the
JSON-Schema parameters from the function:

```python
# my_world.py
from ensemble import register_world, tool


@tool
def lookup_user(user_id: str) -> dict:
    """Return the user record by id."""
    return {"id": user_id, "name": "Alice", "plan": "team"}


@tool
def issue_refund(user_id: str, amount_cents: int) -> dict:
    """Issue a refund for the named user. Returns the refund record."""
    return {"refund_id": "r-1", "user_id": user_id, "amount_cents": amount_cents}


register_world(
    "my_world",
    tools=[lookup_user, issue_refund],
    default_agent_model="claude-sonnet-4-5",
)
```

Then reference the new tools by name in the scenario's
`spawn_agent`:

```python
rep = world.spawn_agent(tools=["lookup_user", "issue_refund"])
```

The `@tool` decorator supports a few shapes:

- `@tool` (bare): infer everything. Use this for tools whose inputs
  are typed primitives, lists, dicts, or `Optional[T]`.
- `@tool(description="...")` etc.: override individual fields, let
  the rest infer. Reach for this when you want a tool name that
  differs from the function name, or a parameters schema the
  inference cannot produce.
- `tool("name", "description", schema, fn)`: the four-argument
  factory, when you have a hand-written schema and want to build a
  `PluginTool` eagerly inside a `register_world(tools=[...])` call.

## Writing a scenario

The scaffolded `scenarios/smoke.py` is the minimum viable shape.
A realistic scenario adds users, opens with a problem, and asks
the agent to do something:

```python
# scenarios/refund.py
import my_world  # noqa: F401  registers the world
from ensemble import scenario


@scenario("my_world.refund", world="my_world")
async def refund(world):
    alice = world.spawn_user(persona="frustrated_customer")
    rep = world.spawn_agent(tools=["lookup_user", "issue_refund"])

    alice.say(rep.id, "I want a refund for the last three months.")

    yield world.until(world.turn_count > 20)
    yield {
        "refund_issued": 1.0 if alice.hidden_goal_resolved() else 0.0,
    }
```

When the scenario has no real user, use `world.opener` for the
seed message and a done-detector for the stop condition:

```python
@scenario("my_world.refactor", world="my_world")
async def refactor(world):
    rep = world.spawn_agent(tools=["read", "edit", "grep", "run_tests"])
    world.opener("rename foo to bar in this file and fix call sites", to=rep.id)

    yield world.until_done(rep.id)
    yield {"completed": 1.0}
```

`world.opener` sends a kickoff message without spawning a user.
`world.until_done` halts when the named agent emits a message
containing `"DONE"` (override with `signal=...` if your agent uses
a different sentinel).

## Comparing many configurations

When the question is "how does this scenario behave across
backends, models, or seeds", reach for the sweep runner. Write a
`sweep.toml`:

```toml
# sweep.toml
[sweep]
scenario = "my_world.refund"
max_parallel = 4
traces_dir = "traces/refund_sweep"

[sweep.flags]
backend = ["mock", "anthropic"]

[sweep.env]
PLANK_SEED = ["1", "2", "3"]
```

Then run:

```sh
ensemble sweep run sweep.toml
```

The runner expands the cartesian product (here, 2 backends x 3
seeds = 6 cells), runs each cell in a subprocess, writes one trace
per cell at `traces/refund_sweep/<cell_id>/trace.jsonl`, and
appends a row per cell to `traces/refund_sweep/index.jsonl`.
Interrupted sweeps resume by default; pass `--no-resume` to force
all cells to re-run.

See `docs/reference/sweeps.md` for the full TOML schema and the
resume semantics.

## What to read next

- [Reference index](reference/index.md) for the contract behind
  every public symbol and CLI flag.
- [Sweeps](reference/sweeps.md) for the matrix runner.
- [Cross-run observability](reference/observability.md) for the
  runs subcommands and the index format.
- [Scenarios](reference/scenarios.md) for the `@scenario`
  decorator, the yield and simulate flavors, and the TOML form.
- [Tools](reference/tools.md) for the `@tool` decorator shapes,
  the JSON-Schema inference rules, and the sandbox semantics.

If you get stuck on a specific error message, search the
reference page for the subsystem you are in: the unhelpful-error
list the audit named has been replaced with messages that name
the fix.
