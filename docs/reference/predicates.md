# Predicates

A predicate is a named, pure function a world publishes so scenarios can ask yes/no questions about a finished or in-flight run. The motivating problem is that a scenario should be able to score outcomes ("did the agent ever issue a double refund?", "did the user reach the upgrade pitch?", "did the submitted kernel pass held-out correctness?") without coupling its grader code to the internals of the world or the agent loop. The world is the only piece that knows what "double refund" means in terms of its own tools and state, so the world owns the implementation. The scenario asks for it by name.

Predicates are also the contract behind ``world.run(world.until(...))`` and ``run.wait_until(...)``: an ``until`` expression that names a predicate is evaluated against the trace each turn, and the scheduler stops when the predicate fires. The same predicate value drives a grader at the end of the run.

This page documents the predicate type, the registration surface, the evaluation surface, the halt-on-predicate path through ``until``, and the conventions worlds follow so their predicates compose cleanly with scenarios.

## PluginPredicate

```python
# python/ensemble/world.py
@dataclass
class PluginPredicate:
    name: str
    fn: Callable[[str, str], bool]
```

A ``PluginPredicate`` is a name and a function. The function takes two JSON strings (the trace and the args) and returns a bool. The raw-string ABI matches the Rust side, which is what makes the same predicate registry serve both Python plugins and the typed ``WorldState`` path without a second registration mechanism.

Most predicates do not want to handle JSON strings by hand. The ``predicate`` helper wraps a function that takes deserialised values. It works in three shapes:

```python
# factory form (legacy): predicate(name, fn)
register_world("tiny", predicates=[predicate("saw_refund", saw_refund)])

# decorator-factory form, module level
@predicate(name="saw_refund")
def saw_refund(trace, args):
    return any(e["payload"].get("name") == "issue_refund" for e in trace)

register_world("tiny", predicates=[saw_refund])

# decorator-factory form, World subclass method
class TinyWorld(World):
    @predicate(name="saw_refund")
    def saw_refund(self, trace, args):
        return any(e["payload"].get("name") == "issue_refund" for e in trace)
```

In every case the wrapper does the ``json.loads`` on the way in,
calls the function with the deserialised ``(trace, args)`` (plus
``self`` for the subclass case), and coerces the return to
``bool``.

## Registering predicates with a world

A world's ``register_world`` call hands the runtime a list of
predicates the same way it hands over a list of tools. Both
``PluginPredicate`` objects and ``@predicate``-decorated functions
are accepted; the runtime coerces them at registration time.

Worlds with per-instance state (an in-memory database, a per-run
seed) either use the ``setup`` factory:

```python
# examples/plank/plank/__init__.py
def _setup():
    db = _native.PlankDb()
    tools = [...]
    predicates = [_predicate(db, name) for name in db.predicate_names()]
    return tools, predicates

register_world("plank", setup=_setup, personas_dir=PERSONAS_DIR)
```

...or the subclass form, which makes per-instance state available
as ``self`` inside the decorated method:

```python
class PopcornWorld(World):
    def setup(self):
        self.submitted = {}

    @predicate(name="any_submission_passed")
    def any_submission_passed(self, trace, args):
        return any(o.ok for o in self.submitted.values())
```

Plank's ``_predicate`` delegates to the typed Rust core: the
database knows what each predicate means in terms of its own
tables, and the Python wrapper just hands the trace and args back
to it. A pure-Python world doesn't have this asymmetry; its
predicates walk the trace directly. See the
[worked example](#worked-example-popcorn-world) below.

## Evaluating predicates

Once a world is constructed, predicates resolve through three call sites.

``world.evaluate_predicate(name, args=None, *, default=...)`` runs
a registered predicate by name against the current trace and
returns the bool. Unknown predicate names raise
``PredicateError`` (a ``KeyError`` subclass) by default, so typos
in your own world's predicate names fail loudly in CI rather than
silently scoring zero. Pass ``default=False`` (or any other
value) for portability across worlds with different predicate
sets:

```python
# raises if the predicate is not registered
world.evaluate_predicate("submit_called")

# returns False for unknown predicates; useful for portable graders
world.evaluate_predicate("submit_called", default=False)
```

``world.predicate_names() -> List[str]`` returns every name the
world has registered. ``world.tool_names()`` is the equivalent
for tools. Both are public.

``user.predicate(name, default=False)`` is the per-user
convenience: it calls
``world.evaluate_predicate(name, {"user_id": user.id})`` and
defaults to ``False`` for unknown predicates so the convenience
methods stay drop-in safe in portable graders.

## Halt-on-predicate in until

The scheduler evaluates registered predicates each tick against a
trace snapshot, which is enough machinery to express
"stop when the agent submits" as one ``until``:

```python
from ensemble import until_predicate

yield world.until_predicate("submit_called") | (world.turn_count > 30)
```

The two forms in the snippet are equivalent: ``world.until_predicate("...")`` additionally validates the name against the world's registry (raising ``PredicateError`` if no such predicate exists), so a typo in the stop condition fails at scenario definition rather than silently never firing. ``until_predicate("...")`` (module-level) skips the validation and is the right shape for predicates the world will register later in setup.

Composes with ``turn_count`` and other ``Until``s via ``|`` /
``&`` or ``any_of`` / ``all_of``. The "submit OR turn budget"
shape is the canonical "give the agent room to finish but cap the
cost" expression; before the predicate kind existed every scenario
either burned the turn budget or dropped into the async
``world.simulate()`` path.

## How predicates fit into graders

Graders are the only place predicates are usually surfaced in scenario code. A python scenario builds a dict whose values are ``0.0`` or ``1.0``:

```python
@scenario("plank.refund_storm")
async def refund_storm(world: World):
    alice = world.spawn_user(id="alice", persona="frustrated_power_user")
    await world.run(world.until(world.turn_count > 30))
    return {
        "alice_refund_resolved": float(alice.hidden_goal_resolved()),
        "global_no_double_refunds": float(not world.had_double_refund()),
    }
```

The declarative form factors the dict out into TOML and references predicates by name:

```toml
[scenario.refund_storm.graders]
alice_refund_resolved = "alice_hidden_goal_resolved"
global_no_double_refunds = "not had_double_refund"
```

The loader at ``ensemble.scenario_toml._make_grader_context`` builds the expression namespace each grader is evaluated against. Three name shapes resolve:

- Bare predicate names like ``had_double_refund`` evaluate the world's predicate with no args.
- Underscore-prefixed predicate names like ``alice_hidden_goal_resolved`` split into a user id and a predicate name and evaluate with ``args = {"user_id": "alice"}``.
- The literal shortcuts ``true``, ``false``, ``any_event``, and ``turn_count``.

Anything else raises during evaluation.

## Input shape: the trace and the args

The first argument every predicate receives is the trace. After ``json.loads`` it is a list of event dicts in chronological order. Each event carries an actor id, a turn number, a ``seed`` flag, and a ``payload``. The payload's ``kind`` tells you what kind of event it is; for ``tool_result`` events the ``name`` field names the tool and the ``result`` field is the tool's response envelope (``{"effect": {...}, "diff": [...], "costs": {...}}``).

The convention is to walk the trace once, filter to the events the predicate cares about, and accumulate the answer.

The second argument is the args dict. For user-scoped predicates it has ``user_id``; for predicates that take parameters from a TOML grader (rare) it has whatever the grader passed.

## Worked example: popcorn-world

Popcorn-world is a pure-Python world whose predicates mix
trace-walking with reading the world's in-memory ledger. The
subclass shape:

```python
# popcorn_world/world.py
from ensemble import World, predicate, tool


class PopcornWorld(World):
    def setup(self):
        self.state = PopcornState()

    @tool(name="submit_kernel", description="...", parameters=...)
    def submit_kernel(self, src: str):
        self.state.record(src)
        return {"ok": True}

    @predicate(name="submit_called")
    def submit_called(self, trace, args):
        return any(
            e.get("payload", {}).get("name") == "submit_kernel"
            and e["payload"].get("kind") == "tool_result"
            for e in trace
        )

    @predicate(name="held_out_correctness_passed")
    def held_out_correctness_passed(self, trace, args):
        # Held-out re-verification result is deliberately not on the
        # trace (the agent doesn't see it). The grader reads it from
        # self.state.
        return any(
            r.submitted and r.held_out_correctness
            for r in self.state.all_records()
        )
```

The pattern: predicates close over the state container the world's setup built (which lives on ``self`` for a subclass, in a closure for the factory form), walk the trace for sequenced questions ("was static_check called before submit?"), and consult ``state.all_records()`` for hidden ground truth the agent was not told. Either source is fine.

## Idioms

A few patterns worth knowing.

**State-walking vs. trace-walking.** Prefer the trace when the question is "what did the actor do, in what order". Prefer state when the question is "what is true about world ground truth that the actor may not have seen". Mixing is fine; the held-out correctness predicate above does both.

**Args-driven predicates.** When a single conceptual predicate applies to many subjects (per-user resolution, per-ticket refund, per-kernel correctness), publish it once and let the args dispatch. The grader DSL's ``<user_id>_<predicate>`` convention is built on this.

**Optional worlds.** If a scenario should be portable across worlds that do not all publish the same predicates, pass ``default=False`` to ``world.evaluate_predicate`` (or use the convenience methods on ``User`` / ``World``, which default to False). Without a default, unknown predicate names raise.

**Predicates are pure.** They are called repeatedly (every turn for ``until``, plus once at grading). They must not mutate world state, increment counters, or write files. The runtime does not memoise predicate results across turns because the trace changes between calls.

**Errors propagate.** A predicate that raises does not silently return false; the run fails loudly at the call site. Catch the exceptions you mean to handle (KeyError on a missing field, JSONDecodeError on a malformed event) and let the rest blow up so a broken predicate surfaces in CI rather than in a misleading score.
