# Tools

A tool is a python function the agent can call. This page covers
the three shapes of the `@tool` decorator, the JSON-Schema
inference rules, the return-envelope contract, and the four
optional capabilities a tool can opt into: resource locks,
timeouts, progress emission, and cost annotations. The last
section covers sandboxed dispatch.

Reach for this page when you are adding a tool, choosing between
the decorator shapes, or wiring a tool's optional capability.

## The common case: bare `@tool`

In the common case the decorator infers everything from the
function. The name comes from the function's `__name__`, the
description from the first paragraph of the docstring, and the
JSON-Schema `parameters` from the type hints. Tools whose inputs
are typed primitives, lists, dicts, or `Optional[T]` should reach
for this form first:

```python
from ensemble import register_world, tool


@tool
def lookup_user(user_id: str) -> dict:
    """Return the user record by id."""
    return {"id": user_id, "name": "Alice", "plan": "team"}


@tool
def search_kb(query: str, tags: list[str] | None = None) -> list:
    """Search the knowledge base. Tags filter the returned items."""
    return kb.search(query, tags or [])


register_world("my_world", tools=[lookup_user, search_kb])
```

The inferred schema for `search_kb` here is:

```json
{
  "type": "object",
  "properties": {
    "query": {"type": "string"},
    "tags": {"type": "array", "items": {"type": "string"}}
  },
  "required": ["query"]
}
```

Parameters with no default become `required`. `Optional[T]` and
default-`None` parameters do not. `Literal["a", "b"]` becomes
`{"enum": ["a", "b"]}`. Unknown types become an empty schema
fragment, which the runtime treats as "any JSON value"; reach for
the override form when you want a tighter schema than inference
can produce.

## Decorator with overrides

When you want to keep inference for most fields but override one
or two (a tool name that differs from the function name, a
description that is not the docstring's first paragraph, a hand-
written schema):

```python
@tool(description="Submit the kernel for static checks and run.")
def submit(src: str):
    return runner.check_and_run(src)
```

Any of `name`, `description`, `parameters`, `timeout_ms`,
`resources`, `sandbox`, `sandbox_world` may be passed. Fields you
omit are inferred from the function.

## Four-argument factory

For tools whose schema is already in hand and you want a
`PluginTool` built eagerly (inside a `register_world(tools=[...])`
call, for example):

```python
register_world("my_world", tools=[
    tool(
        "submit_kernel",
        "Submit the kernel and run it.",
        {"type": "object", "properties": {"src": {"type": "string"}},
         "required": ["src"]},
        submit_kernel_fn,
    ),
])
```

This form does no inference: every field is explicit. It is the
right call when the schema is generated from somewhere else (a
shared spec, a server's tool list) or when the function does not
have type hints.

## `World` subclass methods

Decorated methods on a `World` subclass work the same way. The
subclass walker picks them up at construction time and binds them
to `self`. The bare form is fine because the walker skips `self`
during schema inference:

```python
from ensemble import World, tool


class MyWorld(World):
    def setup(self):
        self.db = Db()

    @tool
    def lookup_user(self, user_id: str) -> dict:
        """Return the user record by id."""
        return {"ok": True, "data": self.db.lookup_user(user_id)}
```

## Return envelope

The wrapper accepts three return shapes:

- A dict, sent as the tool's effect.
- A `(effect, diff)` tuple. The diff is emitted as a `state_diff`
  event alongside the `tool_result`.
- A dict that contains one or more of `effect`, `diff`, `costs`,
  `progress`. This is the structured-envelope form, used when the
  tool wants to annotate cost or emit progress without using the
  `emit_progress` injection path.

Argument routing tries `fn(**args)` first and falls back to
`fn(args)` if the function rejects keyword expansion. A function
that declares an `emit_progress` parameter receives a callable for
progress reporting; see the [progress section](#progress).

## PluginTool

Underneath the decorators, every tool is a `PluginTool`:

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

Most authors never construct this directly; the decorators build
it from the wrapped function.

## Resources

```python
@tool(resources=["billing_db"])
def lookup(user_id: str) -> dict: ...
```

Each name in `resources` is a permit the runtime acquires before
the closure runs and releases when it returns. Two dispatches
that share a name serialise through the world's
`ResourceManager`; unrelated names run in parallel. Resources
can be declared up front via
`register_world(..., resources={"billing_db": 1})` (the
[world api reference](world-api.md#worldtoml-manifest-schema)
covers the manifest form). A name a tool references but no one
declared is created lazily as an exclusive lock on first use.

A `{permits = N}` resource lets up to N concurrent dispatches
hold a permit simultaneously, which is the right shape for a
connection pool or a GPU lane shared across tools.

## Timeouts

```python
@tool(timeout_ms=2000)
def slow(...): ...
```

Caps a single dispatch at the given duration. When the timeout
fires the runtime emits a `tool_timeout` event, the calling agent
sees a tool error, and the scenario continues. The closure runs
on the tokio blocking pool; the timeout is applied around the
`spawn_blocking` future, so a closure that ignores the deadline
still leaks until it completes, but the agent moves on.

Pair `timeout_ms` with progress emission for long-running work so
the trace is observable while the dispatch is in flight.

## Progress

A python tool that declares an `emit_progress` parameter receives
a callable; each invocation records one `(fraction, message)`
pair that the runtime flushes to the trace as a `progress` event
right before the trailing `tool_result`. The helper inspects the
function's signature, so a tool that does not need progress
reporting simply omits the parameter:

```python
@tool
def reconcile_billing(user_id: str, months: int, emit_progress) -> dict:
    """Walk a user's billing history month by month."""
    for i in range(1, months + 1):
        emit_progress(i / months, f"scanned month {i}/{months}")
        ...
    return {"ok": True, "months_reconciled": months}
```

The structured-envelope form (`return {"progress": [...]}`) is
still supported for tools that compute progress in bulk.

## Costs

Tools annotate cost by returning a `costs` dict in their envelope:

```python
def heavy_op(...):
    ...
    return {
        "effect": {"ok": True, "result": result},
        "costs": {"gpu_seconds": 4.2, "usd": 0.03},
    }
```

Units are open strings. Each entry becomes one `cost` event on
the trace, attributed to the calling actor; the runtime maintains
a running total per unit (both world-wide and per-actor). A
budget declared via `world.set_budget(unit, amount, actor=...)`
halts the scheduler with `StopReason::BudgetExceeded` when a
recorded cost would push the total past the cap.

LLM backends annotate cost on their own behalf. Each completion
records `tokens_in`, `tokens_out`, and (when the model is in
`crates/ensemble-runtime/pricing.toml`) `usd` against the actor
that issued the call. The aggregated totals surface on
`RunResult.costs`, `world.cost_summary()`, and the CLI's stdout
summary line for any run that recorded a cost. See the
[runtime reference](runtime.md#usage-and-cost-annotation) for the
pricing table.

## Sandboxed tools

```python
@tool(sandbox=True)
def compile_kernel(src: str): ...
```

A sandboxed tool runs each dispatch in a fresh
`python -m ensemble.tool_worker` subprocess. The worker imports
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
read, use `world.shared_state` (which the runtime serialises into
`ENSEMBLE_SHARED_STATE` on every dispatch). See the
[sandbox contract](world-api.md#sandbox-contract) on the
world-api page for the full cross-boundary table.

`sandbox_world` is the world name the worker imports to
re-register the tool. The wrapper fills it in automatically at
`World(name)` construction time, so authors rarely set it
themselves. Worlds that live outside the worlds registry should
also set `python_package` and `package_dir` on `register_world`
so the worker resolves the same package the parent loaded;
subclass-form worlds derive these automatically from
`type(self).__module__`.
