# Predicates

A predicate is a named, pure function a world publishes so scenarios can ask yes/no questions about a finished or in-flight run. The motivating problem is that a scenario should be able to score outcomes ("did the agent ever issue a double refund?", "did the user reach the upgrade pitch?", "did the submitted kernel pass held-out correctness?") without coupling its grader code to the internals of the world or the agent loop. The world is the only piece that knows what "double refund" means in terms of its own tools and state, so the world owns the implementation. The scenario asks for it by name.

Predicates are also the contract behind `world.run(world.until(...))` and `run.wait_until(...)`: a `until` expression that names a predicate is evaluated against the trace each turn, and the scheduler stops when the predicate fires. The same predicate value drives a grader at the end of the run.

This page documents the predicate type, the registration surface, the evaluation surface, and the conventions worlds follow so their predicates compose cleanly with scenarios. The [world api reference](world-api.md) covers how a world is registered overall; the [scenarios reference](scenarios.md) covers how grader expressions assemble predicate values into scores.

## PluginPredicate

```python
# python/ensemble/world.py
@dataclass
class PluginPredicate:
    name: str
    fn: Callable[[str, str], bool]
```

A `PluginPredicate` is a name and a function. The function takes two JSON strings (the trace and the args) and returns a bool. The raw-string ABI matches the Rust side, which is what makes the same predicate registry serve both Python plugins and the typed `WorldState` path without a second registration mechanism.

Most predicates do not want to handle JSON strings by hand. The `predicate` helper wraps a function that takes deserialised values:

```python
# python/ensemble/world.py
def predicate(name, fn) -> PluginPredicate:
    """fn receives (trace: list[dict], args: dict) and returns a bool."""
```

The helper does the `json.loads` on the way in, calls `fn(trace, args)`, and coerces the return to `bool`. A world that has no reason to touch the raw ABI uses the helper exclusively.

## Registering predicates with a world

A world's `register_world` call hands the runtime a list of predicates the same way it hands over a list of tools. The simplest form lists them inline:

```python
# any world's __init__.py
from ensemble import PluginPredicate, predicate, register_world

def saw_refund(trace, args):
    return any(
        e.get("payload", {}).get("name") == "issue_refund"
        and e["payload"].get("kind") == "tool_result"
        for e in trace
    )

register_world(
    "tiny",
    tools=[...],
    predicates=[predicate("saw_refund", saw_refund)],
)
```

Worlds with per-instance state (an in-memory database, a per-run seed) use the `setup` form instead, building tools and predicates inside a factory the runtime invokes once per `World("tiny")` construction:

```python
# examples/plank/plank/__init__.py
def _setup():
    db = _native.PlankDb()
    tools = [...]
    predicates = [_predicate(db, name) for name in db.predicate_names()]
    return tools, predicates

register_world("plank", setup=_setup, personas_dir=PERSONAS_DIR)
```

Plank's `_predicate` delegates to the typed Rust core: the database knows what each predicate means in terms of its own tables, and the Python wrapper just hands the trace and args back to it. A pure-Python world doesn't have this asymmetry, and its predicates walk the trace directly. See the [worked example](#worked-example-popcorn-world) below.

## Evaluating predicates

Once a world is constructed, predicates resolve through three call sites.

`world.evaluate_predicate(name, args=None) -> Optional[bool]` runs a registered predicate by name against the current trace and returns the bool. The args dict is serialised to JSON and passed through; predicates that ignore args (most of them) receive an empty object. Unknown predicate names return `None` rather than raising, which lets grader code stay robust to optional worlds.

`world.predicate_names() -> List[str]` returns every name the world has registered. Useful at debug time, and used by the TOML grader loader to populate the expression namespace.

`user.predicate(name)` is the per-user convenience: it calls `world.evaluate_predicate(name, {"user_id": user.id})`. A world predicate that wants to answer "did *this user* get their refund?" reads `args["user_id"]` and filters the trace accordingly. The user-scoped form is what `<user_id>_<predicate>` grader names compile down to.

Predicates can be invoked at any time. During a run they drive `world.until(...)` and `run.wait_until(...)`; the scheduler evaluates the named predicate each turn, stops when it returns true, and resumes if you wait against a different condition next. At end of run, the grader phase evaluates predicates one more time against the final trace.

## How predicates fit into graders

Graders are the only place predicates are usually surfaced in scenario code. A python scenario builds a dict whose values are `0.0` or `1.0`:

```python
# any python scenario
@scenario("plank.refund_storm")
async def refund_storm(world: World):
    alice = world.spawn_user(id="alice", persona="frustrated_power_user")
    await world.run(world.until(world.turn_count > 30))
    return {
        "alice_refund_resolved": float(alice.hidden_goal_resolved()),
        "global_no_double_refunds": float(not world.had_double_refund()),
    }
```

The convenience methods on `User` and `World` (`alice.hidden_goal_resolved()`, `world.had_double_refund()`) are thin wrappers around `evaluate_predicate`. Worlds publish them on the proxy objects so scenarios that aren't aware of which predicates exist can still write idiomatic code.

The declarative form factors the dict out into TOML and references predicates by name:

```toml
# examples/plank/scenarios.toml
[scenario.refund_storm.graders]
alice_refund_resolved = "alice_hidden_goal_resolved"
global_no_double_refunds = "not had_double_refund"
```

The loader at `ensemble.scenario_toml._make_grader_context` builds the expression namespace each grader is evaluated against. Three name shapes resolve:

- Bare predicate names like `had_double_refund` evaluate the world's predicate with no args.
- Underscore-prefixed predicate names like `alice_hidden_goal_resolved` split into a user id and a predicate name and evaluate with `args = {"user_id": "alice"}`.
- The literal shortcuts `true`, `false`, `any_event`, and `turn_count`.

Anything else raises during evaluation. A grader that references a predicate the world has not registered fails loudly at the end of the run rather than silently scoring zero.

The grammar around the names is a tiny boolean DSL: `and`, `or`, `not`, parens. Comparisons, attribute access, and arbitrary calls are rejected by the safe evaluator, which means a grader cannot reach back into the world's Python state and a predicate is the only way to surface a custom criterion. The [scenarios reference](scenarios.md#grader-expressions) has the full grammar.

## Input shape: the trace and the args

The first argument every predicate receives is the trace. After `json.loads` it is a list of event dicts in chronological order. Each event carries an actor id, a turn number, a `seed` flag, and a `payload`. The payload's `kind` tells you what kind of event it is; for `tool_result` events the `name` field names the tool and the `result` field is the tool's response envelope (`{"effect": {...}, "diff": [...], "costs": {...}}`). The [traces reference](traces.md) documents every event kind and the payload shape per kind.

The convention is to walk the trace once, filter to the events the predicate cares about, and accumulate the answer. A typical helper looks like:

```python
def _tool_results(trace, tool_name):
    return [
        ev["payload"]["result"]
        for ev in trace
        if ev.get("payload", {}).get("kind") == "tool_result"
        and ev["payload"].get("name") == tool_name
    ]
```

Then a predicate is one or two lines:

```python
def submit_passed(trace, args):
    return any(
        isinstance(r.get("effect"), dict) and r["effect"].get("ok")
        for r in _tool_results(trace, "submit_kernel")
    )
```

The second argument is the args dict. For user-scoped predicates it has `user_id`; for predicates that take parameters from a TOML grader (rare) it has whatever the grader passed. Predicates that ignore args receive `{}` and accept it.

## Worked example: popcorn-world

Popcorn-world is a pure-Python world whose predicates mix trace-walking with reading the world's in-memory ledger. The shape is:

```python
# popcorn_world/popcorn_world/predicates.py
from ensemble import PluginPredicate
from .state import PopcornState

def build_predicates(state: PopcornState) -> List[PluginPredicate]:
    def submit_called(trace_json, args_json):
        trace = json.loads(trace_json) if trace_json else []
        return bool(_tool_results(trace, "submit_kernel"))

    def held_out_correctness_passed(trace_json, args_json):
        # Held-out re-verification result is deliberately not on the
        # trace (the agent doesn't see it). The grader reads it from
        # the world's per-instance ledger.
        return any(
            r.submitted and r.held_out_correctness
            for r in state.all_records()
        )

    return [
        PluginPredicate(name="submit_called", fn=submit_called),
        PluginPredicate(name="held_out_correctness_passed",
                        fn=held_out_correctness_passed),
        # ...
    ]
```

The pattern: predicates close over the state container the world's setup factory built, walk the trace for sequenced questions ("was static_check called before submit?"), and consult `state.all_records()` for hidden ground truth the agent was not told. Either source is fine. The trace is just-the-facts and reproducible from a replay; the world state is fast and can hold information that never reaches the model.

A scenario then references these by name through the grader DSL:

```toml
# popcorn_world/scenarios.toml
[scenario.l1p19_methodical.graders]
submitted = "submit_called"
correct = "submit_passed"
held_out_ok = "held_out_correctness_passed"
lint_hygiene = "not submitted_without_static_check"
```

Each predicate ships its own decision and stays out of the others' business; combining them into a final scorecard is the grader's job, not the predicate's.

## Idioms

A few patterns worth knowing.

**State-walking vs. trace-walking.** Prefer the trace when the question is "what did the actor do, in what order". Prefer state when the question is "what is true about world ground truth that the actor may not have seen". Mixing is fine; the held-out correctness predicate above does both because it cares about a fact (held-out passed) that lives in state but is only meaningful when paired with a trace event (the submission).

**Args-driven predicates.** When a single conceptual predicate applies to many subjects (per-user resolution, per-ticket refund, per-kernel correctness), publish it once and let the args dispatch. The grader DSL's `<user_id>_<predicate>` convention is built on this so the grader does not have to know the user ids ahead of registration.

**Optional worlds.** If a scenario should be portable across worlds that do not all publish the same predicates, call through the user/world convenience methods rather than naming the predicate directly. The convenience methods return `False` for unknown predicates, so a grader that depends on an optional predicate degrades gracefully when run against a world that does not have it.

**Predicates are pure.** They are called repeatedly (every turn for `until`, plus once at grading). They must not mutate world state, increment counters, or write files. If a predicate wants to compute something expensive, cache it on the world's state and let subsequent invocations read the cache. The runtime does not memoize predicate results across turns because the trace changes between calls.

**Errors propagate.** A predicate that raises does not silently return false; the run fails loudly at the call site. Catch the exceptions you mean to handle (KeyError on a missing field, JSONDecodeError on a malformed event) and let the rest blow up so a broken predicate surfaces in CI rather than in a misleading score.
