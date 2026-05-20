# Tools

This page documents how to declare a tool a world's agents can
call. It covers the ``PluginTool`` dataclass at the bottom of the
stack, the ``tool()`` helper that wraps a plain python function for
the common case (in both factory and decorator forms), the
JSON-string envelope the runtime expects, the four optional
capabilities a tool can opt into (resource locks, timeouts,
progress emission, cost annotations), and the sandboxed-dispatch
contract.

## PluginTool

``PluginTool`` is the dataclass ``register_world`` accepts. Every
plugin tool eventually lands as one of these:

```python
@dataclass
class PluginTool:
    name: str
    description: str
    parameters: Dict[str, Any]      # JSON Schema
    fn: Callable[[str], str]        # args_json -> result_json
    timeout_ms: Optional[int] = None
    resources: Optional[List[str]] = None
    sandbox: bool = False
    sandbox_world: Optional[str] = None
```

- ``name`` is what the model sees and what ``register_tool`` keys
  the registry by.
- ``description`` is the LLM-facing hint. Both Anthropic and
  OpenAI forward it to the model verbatim.
- ``parameters`` is the JSON Schema for the tool's argument
  object. The runtime does not enforce the schema; it forwards it
  to the model so the model produces compliant args.
- ``fn`` is the callable the runtime dispatches to. Most authors
  do not write ``fn`` directly; they write a plain python function
  and let ``tool(...)`` wrap it.
- ``timeout_ms``, ``resources``, ``sandbox``, and ``sandbox_world``
  are documented in their own sections below.

## tool() helper

``tool`` works as both a factory and a decorator factory. The
factory form remains the legacy entry point:

```python
# my_world/__init__.py
from ensemble import register_world, tool


def lookup_user(user_id: str):
    user = db.lookup_user(user_id)
    return {"ok": True, "data": user}


register_world(
    "my_world",
    tools=[
        tool(
            name="lookup_user",
            description="Look up a user by id.",
            parameters={
                "type": "object",
                "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"],
            },
            fn=lookup_user,
        ),
    ],
)
```

The decorator-factory form works both at module level and on a
``World`` subclass method. The decorated function is otherwise
unchanged from the caller's perspective; the metadata lives on
``fn._ensemble_tool_meta`` for the runtime to consume:

```python
# my_world/__init__.py
from ensemble import register_world, tool


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
    return {"ok": True, "data": db.lookup_user(user_id)}


register_world("my_world", tools=[lookup_user])
```

```python
# my_world/world.py
from ensemble import World, tool


class MyWorld(World):
    def setup(self):
        self.db = Db()

    @tool(
        name="lookup_user",
        description="Look up a user by id.",
        parameters={
            "type": "object",
            "properties": {"user_id": {"type": "string"}},
            "required": ["user_id"],
        },
    )
    def lookup_user(self, user_id: str):
        return {"ok": True, "data": self.db.lookup_user(user_id)}
```

The wrapper accepts three return shapes:

- A dict, sent as the tool's effect.
- A ``(effect, diff)`` tuple. The diff is emitted as a
  ``state_diff`` event alongside the ``tool_result``.
- A dict that contains one or more of ``effect``, ``diff``,
  ``costs``, ``progress``. This is the structured-envelope form,
  used when the tool wants to annotate cost or emit progress
  without using the ``emit_progress`` injection path.

Argument routing tries ``fn(**args)`` first and falls back to
``fn(args)`` if the function rejects keyword expansion. A function
that declares an ``emit_progress`` parameter receives a callable
for progress reporting; see the [progress section](#progress).

## Effect, diff, costs, progress envelope

The raw ``fn`` callable a ``PluginTool`` carries takes one string
(the JSON of the args dict) and returns one string (the JSON of
the result envelope). The envelope schema:

```json
{
  "effect": <any JSON>,
  "diff":    <any JSON, optional>,
  "costs":   {"<unit>": <number>, ...},
  "progress": [{"fraction": 0..1, "message": "..."}],
  "is_error": <bool, optional; the MCP path uses this>
}
```

The runtime forwards ``effect`` to the calling agent as the tool's
return value. It emits a ``state_diff`` event when ``diff`` is
present, a ``cost`` event per unit in ``costs``, and a ``progress``
event per entry in ``progress`` (flushed in order, ahead of the
trailing ``tool_result``). Any other keys are ignored.

## Resources

```python
@tool(..., resources=["billing_db"])
def lookup(...): ...
```

Each name in ``resources`` is a permit the runtime acquires before
the closure runs and releases when it returns. Two dispatches
that share a name serialise through the world's
``ResourceManager``; unrelated names run in parallel. Resources
can be declared up front via
``register_world(..., resources={"billing_db": 1})`` (the
[world api reference](world-api.md#worldtoml-manifest-schema)
covers the manifest form). A name a tool references but no one
declared is created lazily as an exclusive lock on first use.

A ``Shared{permits = N}`` resource lets up to ``N`` concurrent
dispatches hold a permit simultaneously, which is the right shape
for a connection pool or a GPU lane shared across tools.

## Timeouts

```python
@tool(..., timeout_ms=2000)
def slow(...): ...
```

Caps a single dispatch at the given duration. When the timeout
fires the runtime emits a ``tool_timeout`` event, the calling
agent sees a tool error, and the scenario continues. The closure
runs on the tokio blocking pool; the timeout is applied around
the ``spawn_blocking`` future, so a closure that ignores the
deadline still leaks until it completes, but the agent moves on.

Pair ``timeout_ms`` with progress emission for long-running work
so the trace is observable while the dispatch is in flight.

## Progress

A python tool that declares an ``emit_progress`` parameter
receives a callable; each invocation records one
``(fraction, message)`` pair that the runtime flushes to the
trace as a ``progress`` event right before the trailing
``tool_result``. The helper inspects the function's signature, so
a tool that does not need progress reporting simply omits the
parameter.

```python
@tool(
    name="reconcile_billing",
    description="Walk a user's billing history.",
    parameters={
        "type": "object",
        "properties": {
            "user_id": {"type": "string"},
            "months": {"type": "integer"},
        },
        "required": ["user_id", "months"],
    },
)
def reconcile(user_id: str, months: int, emit_progress):
    for i in range(1, months + 1):
        emit_progress(i / months, f"scanned month {i}/{months}")
        ...
    return {"ok": True, "months_reconciled": months}
```

The structured-envelope form (``return {"progress": [...]}``) is
still supported for tools that compute progress in bulk.

## Costs

Tools annotate cost by returning a ``costs`` dict in their
envelope:

```python
def heavy_op(...):
    ...
    return {
        "effect": {"ok": True, "result": result},
        "costs": {"gpu_seconds": 4.2, "usd": 0.03},
    }
```

Units are open strings. Each entry becomes one ``cost`` event on
the trace, attributed to the calling actor; the runtime maintains
a running total per unit (both world-wide and per-actor). A
budget declared via ``world.set_budget(unit, amount, actor=...)``
halts the scheduler with ``StopReason::BudgetExceeded`` when a
recorded cost would push the total past the cap.

LLM backends also annotate cost on their own behalf. Each
completion records ``tokens_in``, ``tokens_out``, and (when the
model is in ``crates/ensemble-runtime/pricing.toml``) ``usd``
against the actor that issued the call. The
[runtime reference](runtime.md#usage-and-cost-annotation) covers
the pricing table.

## Sandboxed tools

```python
@tool(..., sandbox=True)
def compile_kernel(src: str): ...
```

A sandboxed tool runs each dispatch in a fresh
``python -m ensemble.tool_worker`` subprocess. The worker imports
the world's python package (which re-registers all of the world's
tools, building per-instance state from scratch), looks up the
named tool, calls it once with the supplied JSON args, prints the
result envelope on stdout, and exits. A crash in the worker
surfaces as a structured error envelope rather than killing the
scheduler.

The cost is process startup per dispatch (tens to hundreds of
milliseconds), so sandbox mode pays off only when isolation is
worth more than the latency: tools that run untrusted code, tools
that leave CUDA contexts in a bad state, tools that import a
binary library that occasionally segfaults. State the parent's
closures held is not shared with the worker, so a sandboxed tool
must encode its entire input in its JSON args and its entire
output in its JSON return; for configuration the worker needs to
read, use ``world.shared_state`` (which the runtime serialises
into ``ENSEMBLE_SHARED_STATE`` on every dispatch). See the
[sandbox contract](world-api.md#sandbox-contract) on the
world-api page for the full cross-boundary table.

``sandbox_world`` is the world name the worker imports to
re-register the tool. The wrapper fills it in automatically at
``World(name)`` construction time, so authors rarely set it
themselves. Worlds that live outside the worlds registry should
also set ``python_package`` and ``package_dir`` on
``register_world`` so the worker resolves the same package the
parent loaded; subclass-form worlds derive these automatically
from ``type(self).__module__``.
