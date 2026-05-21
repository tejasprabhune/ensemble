# World API

This page is the reference for authoring a world plugin. A world is
the typed, mutable state a scenario's actors share, the tools that
mutate it, and the predicates that ask questions of the resulting
trace. There are two equivalent ways to declare a world: the
factory form (``register_world(name, ...)``) and the subclass form
(``class MyWorld(World)``). The subclass form is the natural fit
when the world has mutable per-instance state that wants to live on
``self``; the factory form is the natural fit when the state lives
in a typed container the python code only configures (a rust core,
a sqlite handle, a remote service client).

Both paths produce the same trace events, share the same JSON-string
ABI into the rust core, and obey the same sandbox semantics. A
scenario constructed against the world never has to know which path
the world picked.

## Subclassable World

```python
# popcornbench/world.py
from ensemble import World, tool, predicate


class PopcornWorld(World):
    world_name = "popcornbench"

    def setup(self):
        # Per-instance state lives on self. The runtime calls setup()
        # exactly once per construction, before the decorated-method
        # walker runs, so tools and predicates see a fully built
        # state container.
        self.runner = KernelRunner(device="cuda")
        self.submitted: dict[str, dict] = {}

    @tool(
        name="submit_kernel",
        description="Submit a kernel for static check and run.",
        parameters={
            "type": "object",
            "properties": {"src": {"type": "string"}},
            "required": ["src"],
        },
    )
    def submit_kernel(self, src: str):
        outcome = self.runner.check_and_run(src)
        self.submitted[outcome.id] = outcome
        return ({"id": outcome.id, "ok": outcome.ok},
                [{"path": ["submissions", outcome.id], "set": outcome.summary}])

    @predicate(name="submit_called")
    def submit_called(self, trace, args):
        return any(
            e.get("payload", {}).get("name") == "submit_kernel"
            and e["payload"].get("kind") == "tool_result"
            for e in trace
        )

    @predicate(name="any_submission_passed")
    def any_submission_passed(self, trace, args):
        return any(o.ok for o in self.submitted.values())
```

A subclass instance is constructed exactly like the built-in
``World``: ``world = PopcornWorld(backend="anthropic")``. The
runtime walks ``type(world).__mro__`` for methods marked by
``@tool`` and ``@predicate``, builds bound ``PluginTool`` /
``PluginPredicate`` objects, and forwards them into the native tool
and predicate registries. ``setup`` runs before the walker so the
tool wrappers see fully initialised state.

A subclass that wants a per-instance world name distinct from the
class name passes ``world_name = "..."`` as a class attribute, or
overrides the default by passing ``name="..."`` on construction.
The default falls back to the lower-cased class name.

The decorated-method ``self`` is the live ``World`` subclass
instance. Mutate ``self.state`` (or whatever you named it) freely
inside a tool; predicates that close over the same state read the
mutation directly. The sandbox boundary still applies: a tool
declared ``sandbox=True`` does not see in-process mutations made by
its parent, because the sandbox worker is a fresh interpreter. Use
``self.shared_state`` for cross-boundary configuration; the
[sandbox contract](#sandbox-contract) below covers the details.

## register_world (factory form)

```python
# examples/plank/plank/__init__.py
from ensemble import register_world

def _setup():
    db = _native.PlankDb()
    tools = [...]      # list of PluginTool or @tool-decorated fns
    predicates = [...] # list of PluginPredicate or @predicate-decorated fns
    return tools, predicates

register_world(
    "plank",
    setup=_setup,
    personas_dir=PERSONAS_DIR,
    resources={"billing_db": 1},
    python_package="plank",
    package_dir=PACKAGE_DIR,
)
```

The keyword arguments:

- ``setup``: a zero-arg callable returning a ``(tools, predicates)``
  pair. Invoked once per ``World(name)`` construction, so worlds
  with per-instance state (a fresh in-memory SQLite database, a new
  random seed) return a new batch each time. Mutually exclusive
  with ``tools`` / ``predicates``.
- ``tools``: a sequence of ``PluginTool`` or ``@tool``-decorated
  functions for stateless worlds. The decorator-form entries are
  coerced into ``PluginTool`` at registration time, so a top-level
  ``@tool(name=..., description=..., parameters=...)`` function
  can be passed directly without a separate ``tool(...)`` factory
  call.
- ``predicates``: a sequence of ``PluginPredicate`` or
  ``@predicate``-decorated functions; same coercion rules as
  ``tools``.
- ``personas_dir``: a path the persona resolver consults when a
  scenario passes ``persona="..."`` to ``spawn_user``.
- ``resources``: a dict mapping resource name to permit count.
  ``1`` declares an exclusive lock, ``N`` declares a shared
  resource with ``N`` simultaneous holders. The
  ``ResourceManager`` is populated at ``World(name)`` construction.
- ``shared_state``: an initial dict the runtime copies into
  ``world.shared_state`` on every instance. Lives across in-process
  tool calls and is forwarded to sandbox workers via the
  ``ENSEMBLE_SHARED_STATE`` environment variable. See the
  [sandbox contract](#sandbox-contract).
- ``python_package`` and ``package_dir``: explicit hooks the
  sandbox worker uses to re-create this exact world in a fresh
  interpreter. Set them when the world's directory is not in
  ``~/.ensemble/worlds.toml``, when two installations of the same
  package coexist, or when you want the explicit path documented
  in code rather than in a user-local registry.

``register_world`` is idempotent. A second call for the same name
overwrites the prior ``WorldDefinition``, which lets a world
package re-register itself after a hot reload during development.

## Tool and predicate decorators

The same ``tool`` and ``predicate`` helpers work as both factories
and decorators. The factory forms remain:

```python
register_world("ktbench", tools=[
    tool("submit_kernel", "Submit the kernel.", schema, submit_fn),
])
register_world("ktbench", predicates=[
    predicate("submit_called", submit_called_fn),
])
```

The decorator forms work both at module level and on a ``World``
subclass:

```python
# module-level: decorated function is registered directly
@tool(
    name="lookup_user",
    description="Look up a user by id.",
    parameters={
        "type": "object",
        "properties": {"user_id": {"type": "string"}},
        "required": ["user_id"],
    },
    timeout_ms=2000,
)
def lookup_user(user_id: str):
    return {"ok": True, "user": db.lookup(user_id)}

register_world("my_world", tools=[lookup_user])
```

```python
# subclass method: the runtime binds self when materialising the tool
class PopcornWorld(World):
    @predicate(name="submit_passed")
    def submit_passed(self, trace, args):
        return any(o.ok for o in self.submitted.values())
```

The decorator factory accepts the optional ``timeout_ms``,
``resources``, ``sandbox``, and ``sandbox_world`` keyword
arguments. A decorated function is otherwise unchanged from the
caller's perspective (still callable, still has its original
signature); the metadata lives on
``fn._ensemble_tool_meta`` for the runtime to consume.

## Sandbox contract

Tools marked ``sandbox=True`` run in a fresh ``python -m
ensemble.tool_worker`` subprocess. The worker re-imports the
world's python package (rebuilding the entire tool list from
scratch), looks the named tool up, calls it once with the JSON args
the parent forwarded, prints the result envelope on stdout, and
exits. This is the isolation path: a tool that compiles a CUDA
kernel cannot poison the scheduler if it segfaults or leaves the
GPU in a bad state.

The cross-boundary contract is tight on purpose. The boundary is a
fresh interpreter, so:

- **Environment variables cross.** The parent's ``os.environ`` is
  inherited. The runtime adds ``ENSEMBLE_SHARED_STATE`` (a JSON
  dump of ``world.shared_state``) and
  ``ENSEMBLE_SANDBOX_PACKAGE`` / ``ENSEMBLE_SANDBOX_PACKAGE_DIR``
  (the world's importable name and the directory containing it),
  so the worker resolves the same world the parent loaded.
- **The file system crosses.** A tool that wants to read a model
  weight, a problem definition, or a cache lives on disk.
- **Python closure state does NOT cross.** A tool's wrapper in the
  parent process is gone; the worker rebuilds the world from
  scratch. State the parent's ``setup()`` stashed on ``self`` is
  not visible in the worker's ``self``.
- **State diffs are the only durable cross-boundary record.** A
  sandboxed tool that needs the grader to read a result should
  return a ``diff`` in its envelope so the runtime records a
  ``state_diff`` event on the trace. Predicates that depend on
  per-tool ground truth walk the trace for these events instead of
  reading parent-process state.

### shared_state

``world.shared_state`` is a mutable dict on the live ``World``
instance. The runtime serialises it (JSON) into the
``ENSEMBLE_SHARED_STATE`` environment variable on every sandbox
dispatch, so a sandboxed tool can read configuration the parent
set without each world inventing its own env-var convention. The
worker exposes it as ``ensemble.tool_worker.SHARED_STATE``; a
world's ``__init__`` (which the worker re-imports) can consult it
in its ``setup`` factory:

```python
# my_world/__init__.py
import os, json
from ensemble import register_world

def _setup():
    shared = json.loads(os.environ.get("ENSEMBLE_SHARED_STATE", "{}"))
    runner = KernelRunner(device=shared.get("device", "cuda"))
    return _build_tools(runner), _build_predicates(runner)

register_world("my_world", setup=_setup, shared_state={"device": "cuda"})
```

Mutations the worker makes to ``SHARED_STATE`` do not propagate
back; the worker is exiting. Treat the channel as one-way
configuration from parent to worker.

### Subprocess world resolution

When the parent dispatches a sandboxed tool, the worker is
launched as:

```
python -m ensemble.tool_worker --world <name> --tool <name>
```

The worker resolves the world in this order:

1. If ``ENSEMBLE_SANDBOX_PACKAGE`` is set, import that package
   after adding ``ENSEMBLE_SANDBOX_PACKAGE_DIR`` (if set) to
   ``sys.path``. This is the deterministic path the parent uses
   when ``python_package`` / ``package_dir`` were declared on
   ``register_world`` or when the world is a subclass (the
   subclass infers the package from ``type(self).__module__``).
2. Fall back to importing the world name itself as a module.
3. If neither works, exit with code 2 and a message naming the
   world that failed to import.

The fallback to ``~/.ensemble/worlds.toml`` that earlier worker
versions used is gone: it was the source of the "registry says
one world, parent loaded another" failure mode. Worlds that want
the worker to see their package set ``python_package`` /
``package_dir`` on ``register_world``, or use the subclass form
which derives both automatically.

## World.apply

``World.apply(name, **kwargs)`` runs a tool as a system-level
mutation. The runtime records a ``ToolCall``, the registered
tool's ``ToolResult``, and an optional ``StateDiff`` in the trace
with no actor attribution. The events carry ``seed=true`` so a
trace consumer can distinguish setup mutations from agent or user
decisions made during the run.

```python
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

Use ``User.act(tool, **kwargs)`` when the seeded action should
be attributed to a simulated user; use ``World.apply`` when the
mutation has no actor.

## WorldState (rust trait)

The Rust trait is what worlds with a typed rust core implement.
Plank reaches for this so its tool dispatch can keep typed
``PlankCall`` enums and structured ``Diff`` types instead of the
JSON-string ABI. The trait lives in ``ensemble-core``:

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

``apply`` is the single mutation entry point. The runtime wraps the
typed state in a ``WorldHandle<S>`` (cheap to clone, all clones
share the same ``tokio::sync::Mutex<S>``), and
``WorldHandle::apply_and_log(bus, actor, call_id, name, call)``
applies the call, emits ``ToolResult`` + ``StateDiff`` events on
the bus, and returns the typed effect.

``snapshot`` and ``restore`` exist so the runtime can checkpoint a
world before a risky branch and roll back; they are not used by
the scheduler today but are the contract for any future
counterfactual-rollout path.

The Rust trait is not directly reachable from Python; pyo3 cannot
lower generic associated types cleanly. Worlds that want to expose
a typed rust core to Python ship their own thin pyo3 wrapper
(plank's ``PlankDb`` does this in ``examples/plank/python_ext/``).

## world.toml manifest schema

Every world ships a ``world.toml`` at the root of its directory.
The worlds registry (``~/.ensemble/worlds.toml``, managed by
``ensemble worlds add``) reads it to learn where the world's
python package and rust crate live, and what personas and
resources the world expects.

```toml
# examples/plank/world.toml
[world]
name = "plank"
python_package = "plank"
rust_crate = "world"
personas_dir = "personas"
default_user_model = "claude-haiku-4-5"
default_agent_model = "claude-sonnet-4-5"

[[world.default_personas]]
name = "frustrated_power_user"

[[world.default_tools]]
name = "issue_refund"

[world.resources]
billing_db = { permits = 1 }
inference_pool = { permits = 4 }
```

The fields:

- ``world.name`` (required): the short name the registry and
  scenarios use to refer to the world. Must match the name the
  world's python package passes to ``register_world`` (or
  ``world_name`` on the ``World`` subclass).
- ``world.python_package``: the importable name of the world's
  python package, defaulting to ``world.name``.
- ``world.rust_crate``: the relative path (or crate name) of the
  world's rust crate. Informational; the build system finds the
  crate through the maturin manifest, not this field.
- ``world.personas_dir``: directory holding ``*.toml`` persona
  files relative to the manifest.
- ``world.default_personas`` and ``world.default_tools``: lists of
  names the world ships. Informational; used by
  ``ensemble worlds show``.
- ``world.default_user_model`` and ``world.default_agent_model``:
  the models ``spawn_user()`` and ``spawn_agent()`` use when the
  scenario does not pass ``model=...`` explicitly. Lets a world
  pick its preferred defaults without every scenario repeating the
  model identifier on every spawn line.
- ``world.resources``: table mapping resource name to a
  ``{permits = N}`` declaration. Declared at world load time so a
  shared resource is actually shared rather than silently
  downgraded to exclusive on first use.
- ``world.cli``: reserved for world-specific CLI subcommands.

`register_world` accepts the same two model fields as kwargs for
worlds that prefer to declare them in Python rather than TOML:

```python
register_world(
    "my_world",
    tools=[...],
    default_user_model="claude-haiku-4-5",
    default_agent_model="claude-sonnet-4-5",
)
```

The manifest loader (``ensemble.load_world_manifest``) returns a
``WorldManifest`` dataclass. The ``ensemble worlds add`` command
validates the manifest at registration time. The python entry
point also auto-discovers a ``world.toml`` in the cwd at run
time, so a scaffold-and-run flow does not require the manual
`ensemble worlds add` step.
