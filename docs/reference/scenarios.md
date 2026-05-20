# Scenarios

This page is the reference for the scenario surface: the
``@scenario`` decorator, the ``World`` instance the scenario
function binds against, and the ``scenarios.toml`` declarative
manifest.

## @scenario decorator

``@scenario(name, *, world=None)`` registers a python coroutine as
a runnable scenario under the global registry. The decorator
accepts two flavours of function: an async generator that yields
its until predicate and then its grader scores, and a regular
async function that runs the simulation itself and returns the
grader dict.

```python
# yield flavor; the typical case
@scenario("plank.smoke", world="plank")
async def smoke(world):
    user = world.spawn_user(id="alice", persona="patient_retail")
    world.spawn_agent(id="rep", model="claude-sonnet-4-5", tools=["search_kb"])
    user.say("rep", "hi")
    yield world.until(world.turn_count > 8)
    yield {"saw_event": 1.0}
```

```python
# async-def flavor; required when the scenario drives the run itself
@scenario("plank.audit", world="plank")
async def audit(world):
    carol = world.spawn_user(id="carol", persona="enterprise_admin")
    world.spawn_agent(id="rep", model="claude-sonnet-4-5", tools=["escalate"])
    carol.say("rep", "audit log please")
    async with world.simulate() as run:
        await run.wait_until(world.turn_count > 10)
        carol.say("rep", "escalate this")
        await run.wait_until(world.turn_count > 18)
    return {"escalated": 1.0 if carol.hidden_goal_resolved() else 0.0}
```

Parameters:

- ``name`` (required): the registry key. ``run_scenario(name)`` and
  ``ensemble run <name>`` look the scenario up by this string. The
  convention is ``<world>.<short_name>``, but any string works.
- ``world``: the world name this scenario defaults to. The runner
  uses it when ``--world`` is not supplied on the CLI, or when
  ``run_scenario(name, world_name=None)`` is called.

The wrapper the decorator returns accepts these keyword arguments
when invoked:

- ``world_name``: overrides the decorator's ``world=`` default.
- ``backend``: the LLM backend, one of ``"mock"``, ``"anthropic"``,
  ``"openai"``, ``"vllm"``, or ``"auto"``.
- ``base_url``: optional override for the backend's base URL.
- ``trace_path``: when supplied, the runtime mirrors every event
  to this JSONL file as it is appended.
- ``external_agent_id``: names the agent slot that the connected
  MCP client drives, used by the
  ``mcp serve --scenario --as-agent`` path.
- ``on_world_constructed``: a callable invoked with the
  constructed ``World`` instance once it is built but before the
  scenario function runs.

The wrapper returns a ``RunResult`` dataclass with three fields:
``name`` (the scenario name), ``scores`` (the grader dict), and
``trace`` (the parsed event log).

## World

``World(name, *, backend=None, base_url=None, dotenv=True,
verbose=None, trace_path=None, external_agent_id=None)`` is the
scenario-facing wrapper around the native rust world. Construction
is what triggers the world plugin to register itself: the python
package named in ``world.toml`` must already have been imported
(a common pattern is ``import plank`` at the top of the scenario
module so plank's ``register_world`` runs before the World is
built).

A ``World`` subclass (``class PopcornWorld(World)``) auto-derives
its world name from the ``world_name`` class attribute or the
lower-cased class name; its decorated methods and ``setup(self)``
are wired up at instance construction time. The
[world-api reference](world-api.md) covers the subclass form in
detail.

The properties:

- ``world.name``: the world's registry name.
- ``world.backend``: the chosen backend's string name.
- ``world.turn_count``: a sentinel that produces an ``Until`` from
  ``> N`` or ``>= N`` comparisons.
- ``world.trace_path``: the current live-trace sink path, or
  ``None`` when no sink is attached.
- ``world.users`` / ``world.agents``: lists of ``User`` / ``Agent``
  proxies the scenario has spawned, in declaration order.
- ``world.shared_state``: a mutable dict the runtime forwards into
  sandbox workers via ``ENSEMBLE_SHARED_STATE``. See the
  [sandbox contract](world-api.md#sandbox-contract).

The methods documented in their own subsections below:
``spawn_user``, ``spawn_agent``, ``until``, ``until_predicate``,
``run``, ``simulate``, ``trace``, ``apply``, ``log_note``,
``log_event``, ``actor_hidden_state``, ``tool_names``,
``predicate_names``, ``evaluate_predicate``, ``set_budget``,
``cost_total``, ``record_cost``, ``set_trace_path``.

## spawn_user

```python
world.spawn_user(
    id=None,
    persona=None,
    hidden_goal=None,
    model="user-model",
    system_prompt=None,
    hidden_state=None,
) -> User
```

Creates a ``User`` actor and records it on the world. Emits a
``user_spawned`` system event to the trace with the resolved
persona, model, and hidden state so trace consumers can render
the actor's framing without each scenario logging it manually.

Returns a ``User`` proxy whose methods include ``id``,
``persona``, ``hidden_state``, ``backend_info``, ``say``, ``act``,
``predicate`` (per-user predicate convenience), and the
``hidden_goal_resolved`` / ``was_redirected_to_upgrade``
shortcuts. When the persona has ``mode = "trained"`` together
with an ``adapter_name``, ``spawn_user`` routes the actor through
a per-user ``LocalAdapterBackend``.

## spawn_agent

```python
world.spawn_agent(
    id=None,
    model="claude-sonnet-4-5",
    tools=None,
    system_prompt=None,
    params=None,
) -> Agent
```

Creates an ``Agent`` actor backed by the world's shared LLM
backend and the world's tool registry, restricted to the named
tools. Emits an ``agent_spawned`` system event with
``actor_id``, ``model``, ``tools``, ``system_prompt``, and
``params`` so the trace viewer can render the spawn special and
worlds no longer need a per-scenario ``log_note`` helper for the
system prompt.

- ``id``: the actor id. Defaults to ``"agent"`` when unset.
- ``model``: the model identifier sent to the backend.
- ``tools``: tool restriction. ``None`` means the agent sees every
  tool the world registered; ``[]`` means no tools; a non-empty
  list filters both the schemas the model sees and the
  dispatcher's accept-list.
- ``system_prompt``: explicit system prompt.
- ``params``: an open dict of per-agent LLM knobs forwarded into
  the backend's ``CompletionRequest`` as ``extra_params``. Useful
  for ``reasoning_effort``, ``top_p``, or any other
  provider-specific extension on a single agent. Backends ignore
  keys they do not understand; the underlying API rejects bad
  values with its own error.

When ``id`` matches the world's ``external_agent_id`` (set on
construction), ``spawn_agent`` returns an ``_ExternalAgent`` proxy
instead of building a real agent. The MCP-connected client drives
the slot.

## world.until, world.until_predicate, and the turn_count sentinel

``world.until(condition)`` coerces a condition into an ``Until``
value the scheduler can evaluate. Accepts:

- an existing ``Until``, returned unchanged;
- a comparison built from ``world.turn_count > N`` or ``>= N``.

Boolean values are explicitly rejected because they almost always
indicate a mistake.

``world.until_predicate(name, **args)`` builds an ``Until`` that
fires when the named world predicate returns true on the live
trace. The scheduler evaluates the predicate each tick against a
trace snapshot, so the "stop when the agent submits" pattern is
one expression rather than a turn-count budget plus a post-hoc
inspection:

```python
from ensemble import until_predicate

# stop on submission, give up after 30 turns either way
yield world.until_predicate("submit_called") | (world.turn_count > 30)
```

``until_predicate(name, **args)`` (module-level) is the same
factory; ``world.until_predicate`` additionally validates that the
predicate is registered, raising ``PredicateError`` if the name is
unknown, so a typo in your stop condition fails loudly at scenario
definition rather than silently never firing.

Combinators come in two forms:

```python
from ensemble import any_of, all_of, until_predicate

yield world.until(any_of(world.turn_count > 30, until_predicate("submit_called")))
yield world.until(all_of(world.turn_count >= 4, until_predicate("hit_goal")))

# the | and & operators mean the same thing
yield world.until((world.turn_count > 30) | until_predicate("submit_called"))
```

``any_of`` and ``all_of`` flatten nested calls of the same
combinator, so deeply nested expressions stay readable on the
wire.

## world.run and world.simulate

``world.run(until)`` is the blocking entry point. The scheduler
runs on the global tokio runtime; the calling thread blocks on
``block_on`` until the scheduler halts. Returns the parsed trace.

```python
trace = world.run(world.until(world.turn_count > 8))
```

If the scenario forgot to seed a message (no ``user.say`` and no
``agent.say``) and only agents are registered, the runtime emits a
``system`` note explaining that the scheduler will quiesce on the
first tick. The run still proceeds in case the scenario meant to
start idle; the warning surfaces the most common
"empty-trace-with-no-error" trap.

``world.simulate()`` returns an async context manager that starts
the scheduler on a background task and exposes a ``SimulationRun``
handle inside the block:

```python
async with world.simulate() as run:
    fired = await run.wait_until(world.turn_count > 4, timeout_ms=15_000)
    # ... inspect state, push new messages, mutate the world ...
```

``run.wait_until(condition, timeout_ms=30_000)`` returns ``True``
when the predicate fires and ``False`` when the timeout elapses.
Use the async-with path when the scenario needs to react to what
happened earlier in the run; use ``world.run`` when the scenario
commits to a single stop condition up front.

## world.apply

See the [world api reference](world-api.md#worldapply). ``apply``
is the system-level mutation path: it runs a tool with no actor
attribution and stamps ``seed=true`` on the resulting events.

## world.log_note and world.log_event

``world.log_note(text)`` appends a free-form system note to the
trace. ``world.log_event(kind, payload)`` is the structured
counterpart: it appends a system note whose body is
``{"kind": kind, **payload}``, which lets the viewer render known
kinds (``agent_spawned``, ``user_spawned``, ``grader``,
``problem_prompt``) specially and falls back to a generic notes
panel for unknown kinds. Both are public; the trace recorder's
internals are intentionally not.

```python
world.log_event("problem_prompt", {"text": problem_text})
world.log_event("eval_config", {"seed": 42, "shots": 5})
```

## evaluate_predicate, predicate_names, tool_names

``world.evaluate_predicate(name, args=None, *, default=...)`` runs
a registered predicate against the current trace. Unknown
predicate names raise ``PredicateError`` by default so typos in
your own world's predicate names fail loudly in CI. Pass
``default=False`` (or any other value) for portability across
worlds with different predicate sets.

``world.predicate_names()`` returns every name the world has
registered. ``world.tool_names()`` does the same for tools. Both
are public; a generic scenario that wants to spawn an agent with
"everything the world has" can write
``world.spawn_agent(id="r", tools=world.tool_names())``.

## actor_hidden_state

``world.actor_hidden_state(actor_id)`` returns the live hidden
state dict for the named user (or an empty dict for agents
without hidden state). Useful for graders that read a reviewer
agent's verdict after the run completes:

```python
verdict = world.actor_hidden_state("reviewer").get("verdict", "pending")
return {"verdict_was_reject": float(verdict == "reject")}
```

## scenarios.toml schema

The declarative form covers scenarios that don't need mid-run
intervention. ``ensemble.load_manifest(path)`` parses the file and
registers a scenario per entry in the global registry, so the same
runner code drives both python ``@scenario`` and TOML scenarios.

```toml
# examples/plank/scenarios.toml
[scenario.refund_storm]
world = "plank"
duration_turns = 30
seed = 42

[[scenario.refund_storm.users]]
id = "alice"
persona = "frustrated_power_user"
hidden_goal = "refund_3mo"
model = "user-model"
initial_action = { tool = "open_ticket", args = { ticket_id = "t-100", user_id = "u-alice", subject = "want my money back" } }

[[scenario.refund_storm.agents]]
id = "rep1"
model = "claude-sonnet-4-5"
tools = ["lookup_user", "issue_refund", "escalate", "search_kb"]

[scenario.refund_storm.graders]
alice_refund_resolved = "alice_hidden_goal_resolved"
global_no_double_refunds = "not had_double_refund"
```

Per-scenario fields:

- ``world`` (required): the world name to construct.
- ``duration_turns``: integer used as ``world.turn_count > N`` for
  the until predicate. Defaults to 20.
- ``seed``: informational; not consumed by the runtime today.
- ``users``: array of user specs. Each entry supplies ``id``,
  ``persona``, optional ``hidden_goal``, optional ``model``, and
  an optional ``initial_action = { tool = "...", args = {...} }``.
- ``agents``: array of agent specs. ``id``, ``model``, ``tools``
  get passed to ``world.spawn_agent``.
- ``graders``: a table of name => grader expression. Each
  expression is evaluated against the context the loader builds;
  truthy values become ``1.0``, falsy ``0.0``.

## Grader expressions

Grader expressions live in a tiny boolean DSL: ``and``, ``or``,
``not``, parens, and bare names. Calls, attribute access, and
literals other than ``true``/``false`` are rejected. The scenario
loader builds a context that includes:

- ``true``, ``false``, ``any_event``, ``turn_count`` (the literal
  shortcuts).
- Every world predicate by name, evaluated against the trace.
- Per-user predicates as ``<user_id>_<predicate>``, evaluated with
  ``args = {"user_id": "<user_id>"}``.

Unknown names raise rather than silently returning false, so a
typo in a grader expression fails loudly at run time.
