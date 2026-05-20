# World API

This page is the reference for authoring a world plugin. A world is
the typed, mutable state a scenario's actors share, the tools that
mutate it, and the predicates that ask questions of the resulting
trace. The Python plugin path is what most worlds use; the Rust
`WorldState` trait is available for worlds whose state is best
expressed as a typed Rust core. Both paths produce the same trace
events, so a scenario does not change shape based on which one a
world picked.

## register_world

A world's Python package calls `register_world` exactly once at
import time. The registry is global to the process; importing
`plank` (or any other world's package) is what makes
`World("plank")` resolve later.

```python
# examples/plank/plank/__init__.py
from ensemble import PluginTool, PluginPredicate, register_world

def _setup():
    db = _native.PlankDb()
    tools = [...]      # PluginTool list
    predicates = [...] # PluginPredicate list
    return tools, predicates

register_world(
    "plank",
    setup=_setup,
    personas_dir=PERSONAS_DIR,
    resources={"billing_db": 1},
)
```

The keyword arguments:

- `setup`: a zero-arg callable returning a `(tools, predicates)`
  pair. Invoked once per `World(name)` construction, so worlds with
  per-instance state (a fresh in-memory SQLite database, a new
  random seed) return a new batch each time. Mutually exclusive
  with `tools`/`predicates`.
- `tools`: a sequence of `PluginTool`, used when the world's tool
  set is stateless. Mutually exclusive with `setup`.
- `predicates`: a sequence of `PluginPredicate`, with the same
  stateless semantics as `tools`.
- `personas_dir`: a path the persona resolver consults when a
  scenario passes `persona="..."` to `spawn_user`. The path is also
  the namespace key under which the world's personas live, so a
  scenario constructed against this world name finds the right
  TOMLs.
- `resources`: a dict mapping resource name to permit count. `1`
  declares an exclusive lock, `N` declares a shared resource with
  `N` simultaneous holders. The runtime calls
  `ResourceManager.declare` for each entry at `World(name)`
  construction. Tools that reference a resource by name in
  `Tool.with_resources` still get the lazy exclusive declaration if
  the manifest did not pre-declare it.

`register_world` is idempotent. A second call for the same name
overwrites the prior `WorldDefinition`, which lets a world package
re-register itself after a hot reload during development.

## WorldState (python plugin path)

A Python world's state lives inside the closures its tools hold.
There is no `WorldState` class to subclass on the Python side; the
world expresses itself entirely through the tools `register_world`
returns. Each tool's callable takes a JSON string of arguments and
returns a JSON string envelope; the [tools reference](tools.md)
documents the envelope schema and the `tool()` helper that hides it
for the common case.

A tool that returns a `diff` in its envelope emits a `state_diff`
event on the trace alongside its `tool_result`. That is the
mechanism the trace viewer's state-changes panel reads, and the
mechanism predicates use to detect concrete world mutations. A
scenario that wants to mutate state outside of an actor's turn (a
test fixture, a scheduled world event, a deterministic seed action
that does not belong to any user) calls `World.apply` instead.

## World.apply

`World.apply(name, **kwargs)` runs a tool as a system-level
mutation. The runtime records a `ToolCall`, the registered tool's
`ToolResult`, and an optional `StateDiff` in the trace with no
actor attribution. The events carry `seed=true` so a trace consumer
can distinguish setup mutations from agent or user decisions made
during the run.

```python
# any python session
from ensemble import World

world = World("plank", backend="mock")
out = world.apply(
    "open_ticket",
    ticket_id="t-seed-1",
    user_id="u-alice",
    subject="seeded by scenario setup",
)
# out is {"effect": {...}, "diff": [...]}
```

This is the Python equivalent of the Rust `WorldHandle::apply_and_log`
path. Use `User.act(tool, **kwargs)` when the seeded action should
be attributed to a simulated user; use `World.apply` when the
mutation has no actor.

## WorldState (rust trait)

The Rust trait is what worlds with a typed rust core implement.
Plank reaches for this so its tool dispatch can keep typed
`PlankCall` enums and structured `Diff` types instead of the
JSON-string ABI. The trait lives in `ensemble-core`:

```rust
// crates/ensemble-core/src/world.rs
pub trait WorldState: Send + Sync + 'static {
    type ToolCall: DeserializeOwned + Send;
    type ToolEffect: Serialize + Send;
    type Diff: Serialize + Send;

    fn apply(
        &mut self,
        call: Self::ToolCall,
    ) -> Result<(Self::ToolEffect, Self::Diff), ToolError>;

    fn snapshot(&self) -> Vec<u8>;
    fn restore(&mut self, snapshot: &[u8]) -> Result<(), RestoreError>;
}
```

`apply` is the single mutation entry point. The runtime wraps the
typed state in a `WorldHandle<S>` (cheap to clone, all clones share
the same `tokio::sync::Mutex<S>`), and
`WorldHandle::apply_and_log(bus, actor, call_id, name, call)`
applies the call, emits `ToolResult` + `StateDiff` events on the
bus, and returns the typed effect.

`snapshot` and `restore` exist so the runtime can checkpoint a
world before a risky branch and roll back; they are not used by the
scheduler today but are the contract for any future
counterfactual-rollout path.

The Rust trait is not directly reachable from Python; pyo3 cannot
lower generic associated types cleanly. Worlds that want to expose
a typed rust core to Python ship their own thin pyo3 wrapper
(plank's `PlankDb` does this in `examples/plank/python_ext/`).

## world.toml manifest schema

Every world ships a `world.toml` at the root of its directory. The
worlds registry (`~/.ensemble/worlds.toml`, managed by
`ensemble worlds add`) reads it to learn where the world's python
package and rust crate live, and what personas and resources the
world expects.

```toml
# examples/plank/world.toml
[world]
name = "plank"
python_package = "plank"
rust_crate = "world"
personas_dir = "personas"

[[world.default_personas]]
name = "frustrated_power_user"

[[world.default_tools]]
name = "issue_refund"

[world.resources]
billing_db = { permits = 1 }
inference_pool = { permits = 4 }
```

The fields:

- `world.name` (required): the short name the registry and
  scenarios use to refer to the world. Must match the name the
  world's python package passes to `register_world`.
- `world.python_package`: the importable name of the world's
  python package, defaulting to `world.name`. The CLI's `run` and
  `mcp` subcommands import this package to trigger
  `register_world` before the scenario constructs `World(name)`.
- `world.rust_crate`: the relative path (or crate name) of the
  world's rust crate. Informational; the build system finds the
  crate through the maturin manifest, not this field.
- `world.personas_dir`: directory holding `*.toml` persona files
  relative to the manifest. The persona resolver indexes by this
  directory when a scenario passes `persona="name"` to
  `spawn_user`.
- `world.default_personas`: list of persona names the world ships.
  Used by `ensemble worlds show` and reserved for future use by
  tooling; the runtime does not enforce that a scenario must use
  one of them.
- `world.default_tools`: parallel list of tool names. Same
  informational role.
- `world.resources`: table mapping resource name to a
  `{permits = N}` declaration. Declared at world load time so a
  shared resource is actually shared rather than silently
  downgraded to exclusive on first use.
- `world.cli`: reserved for world-specific CLI subcommands.
  Tolerated by the parser, not yet wired into the runtime.

The manifest loader (`ensemble.load_world_manifest`) returns a
`WorldManifest` dataclass. The `ensemble worlds add` command
validates the manifest at registration time, so a typo in the
manifest is caught before the first scenario run.
