# Scenarios

This page is the reference for the scenario surface: the
`@scenario` decorator, the `World` instance the scenario function
binds against, and the `scenarios.toml` declarative manifest.

## @scenario decorator

`@scenario(name, *, world=None)` registers a Python coroutine as a
runnable scenario under the global registry. The decorator accepts
two flavours of function: an async generator that yields its until
predicate and then its grader scores, and a regular async function
that runs the simulation itself and returns the grader dict.

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

- `name` (required): the registry key. `run_scenario(name)` and
  `ensemble run <name>` look the scenario up by this string. The
  convention is `<world>.<short_name>`, but any string works.
- `world`: the world name this scenario defaults to. The runner
  uses it when `--world` is not supplied on the CLI, or when
  `run_scenario(name, world_name=None)` is called. A scenario can
  be invoked against a different world by passing one explicitly.
  Default is `None`, which falls back to the built-in `"noop"`
  world.

The wrapper the decorator returns accepts these keyword arguments
when invoked:

- `world_name`: overrides the decorator's `world=` default.
- `backend`: the LLM backend, one of `"mock"`, `"anthropic"`,
  `"openai"`, `"vllm"`, or `"auto"`. See the [runtime
  reference](runtime.md) for the auto-precedence rules.
- `base_url`: optional override for the backend's base URL. Honors
  `ANTHROPIC_BASE_URL` and `OPENAI_BASE_URL` from the environment
  when unset.
- `trace_path`: when supplied, the runtime mirrors every event to
  this JSONL file as it is appended. The sink opens in append
  mode; pre-existing contents are preserved.
- `external_agent_id`: names the agent slot that the connected
  MCP client drives, used by the `mcp serve --scenario --as-agent`
  path. When set, `spawn_agent(id=external_agent_id, ...)` produces
  an `ExternalAgent` proxy instead of a real LLM-backed agent.
- `on_world_constructed`: a callable invoked with the constructed
  `World` instance once it is built but before the scenario
  function runs. Used by the MCP entry point to capture the world
  for handoff to the MCP server; most callers leave it unset.

The wrapper returns a `RunResult` dataclass with three fields:
`name` (the scenario name), `scores` (the grader dict), and
`trace` (the parsed event log).

## World

`World(name, *, backend=None, base_url=None, dotenv=True,
verbose=None, trace_path=None, external_agent_id=None)` is the
scenario-facing wrapper around the native rust world. Construction
is what triggers the world plugin to register itself: the python
package named in `world.toml` must already have been imported (a
common pattern is `import plank` at the top of the scenario module
so plank's `register_world` runs before the World is built).

The properties:

- `world.name`: the world's registry name.
- `world.backend`: the chosen backend's string name.
- `world.turn_count`: a sentinel that produces an `Until` from
  `> N` or `>= N` comparisons. Use it in `world.until(...)` to
  express stop conditions; the int value is the count of events
  in the log so far, available via `int(world.turn_count)`.
- `world.trace_path`: the current live-trace sink path, or `None`
  when no sink is attached.
- `world.users` / `world.agents`: lists of `User` / `Agent`
  proxies the scenario has spawned, in declaration order.

The methods documented in their own subsections below:
`spawn_user`, `spawn_agent`, `until`, `run`, `simulate`, `trace`,
`apply`, `set_budget`, `cost_total`, `record_cost`,
`evaluate_predicate`, `predicate_names`, `set_trace_path`.

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

Creates a `User` actor and records it on the world. Returns a
`User` proxy whose methods are documented below.

- `id`: the actor id. When unset, defaults to the persona name if
  supplied, otherwise `"user"`. Used as the `actor` field on every
  event the user produces, so distinct ids are required to tell
  users apart in the trace.
- `persona`: a TOML name registered in the world's
  `personas_dir`. The resolver pulls the system prompt template
  and the default hidden state from the file. Missing personas
  resolve to `None` and the user gets the model's default
  behaviour.
- `hidden_goal`: a string written into the user's hidden state
  under the `hidden_goal` key. Useful as a one-shot override of
  the persona's default goal.
- `model`: the model identifier the user actor sends to the
  backend. For the mock backend this is the script-lookup key; for
  real backends it is the provider's model name (`"claude-haiku-4-5"`,
  `"gpt-5"`).
- `system_prompt`: an explicit system prompt that overrides
  whatever the persona resolved. Most scenarios leave this alone
  and let the persona file drive the prompt.
- `hidden_state`: a dict merged into the persona's default hidden
  state. Wins on top of the file defaults, so a scenario that
  wants to nudge a single field passes just that field rather than
  the whole state.

When the persona has `mode = "trained"` together with an
`adapter_name`, `spawn_user` routes the actor through a per-user
`LocalAdapterBackend`. The base URL comes from
`persona.training.serve_url` if set, otherwise the
`ENSEMBLE_VLLM_BASE_URL` environment variable. The
[personas reference](personas.md) has the full auto-wiring
contract.

The returned `User`:

- `user.id` (string)
- `user.persona` (`PersonaSpec` or `None`)
- `user.hidden_state` (dict snapshot of the current hidden state)
- `user.backend_info` (dict describing the resolved per-user
  backend, or `None` when the user shares the world's default)
- `user.say(target_id, text)` (queue a seed message)
- `user.act(tool_name, **kwargs)` (run a tool as the user; events
  are marked `seed=true`)
- `user.predicate(name)` (evaluate a world predicate against the
  trace with `args={"user_id": user.id}`)
- `user.hidden_goal_resolved()` / `user.was_redirected_to_upgrade()`
  (convenience predicates the worked example uses)

## spawn_agent

```python
world.spawn_agent(
    id=None,
    model="claude-sonnet-4-5",
    tools=None,
    system_prompt=None,
) -> Agent
```

Creates an `Agent` actor backed by the world's shared LLM backend
and the world's tool registry, restricted to the named tools.

- `id`: the actor id. Defaults to `"agent"` when unset.
- `model`: the model identifier sent to the backend.
- `tools`: tool restriction. `None` (the default) means the agent
  sees every tool the world registered. `[]` means the agent has
  no tools; any hallucinated call lands in the trace as an
  is_error tool result. A non-empty list filters the schemas the
  model sees and the dispatcher's accept-list.
- `system_prompt`: explicit system prompt. Unset means the model
  runs against the backend's default behaviour.

When `id` matches the world's `external_agent_id` (set on
construction), `spawn_agent` returns an `_ExternalAgent` proxy
instead of building a real agent. The MCP-connected client drives
the slot; the proxy's `say` routes through
`world._native.external_send_as`.

The returned `Agent`:

- `agent.id` (string)
- `agent.say(target_id, text)` (queue a seed message)

## world.until and the turn_count sentinel

`world.until(condition)` coerces a condition into an `Until` value
the scheduler can evaluate. Accepts:

- an existing `Until`, returned unchanged;
- a comparison built from `world.turn_count > N` or `>= N`, which
  the sentinel converts into a JSON-spec `Until` on the fly.

Boolean values are explicitly rejected because they almost always
indicate a mistake (`int(world.turn_count) > N` produces a `bool`,
not an `Until`; the scenario means `world.turn_count > N`).

Combinators come in two forms:

```python
from ensemble import any_of, all_of

# any_of fires when any sub-condition holds; all_of fires when all do
yield world.until(any_of(world.turn_count > 30, condition_b))
yield world.until(all_of(world.turn_count >= 4, condition_c))

# the | and & operators mean the same thing
yield world.until((world.turn_count > 30) | condition_b)
yield world.until((world.turn_count > 30) & condition_c)
```

`any_of` and `all_of` flatten nested calls of the same combinator,
so deeply nested expressions stay readable on the wire.

## world.run and world.simulate

`world.run(until)` is the blocking entry point. The scheduler runs
on the global tokio runtime; the calling thread blocks on
`block_on` until the scheduler halts. Returns the parsed trace.

```python
trace = world.run(world.until(world.turn_count > 8))
```

`world.simulate()` returns an async context manager that starts
the scheduler on a background task and exposes a `SimulationRun`
handle inside the block:

```python
async with world.simulate() as run:
    fired = await run.wait_until(world.turn_count > 4, timeout_ms=15_000)
    # ... inspect state, push new messages, mutate the world ...
```

`run.wait_until(condition, timeout_ms=30_000)` returns `True` when
the predicate fires and `False` when the timeout elapses. The
scheduler keeps running either way, so a False return can be
followed by another wait against a different condition. Use the
async-with path when the scenario needs to react to what happened
earlier in the run; use `world.run` when the scenario commits to a
single stop condition up front.

## world.apply

See the [world api reference](world-api.md#worldapply). `apply` is
the system-level mutation path: it runs a tool with no actor
attribution and stamps `seed=true` on the resulting events.

## scenarios.toml schema

The declarative form covers scenarios that don't need mid-run
intervention. `ensemble.load_manifest(path)` parses the file and
registers a scenario per entry in the global registry, so the same
runner code drives both python `@scenario` and TOML scenarios.

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

- `world` (required): the world name to construct.
- `duration_turns`: integer used as `world.turn_count > N` for the
  until predicate. Defaults to 20.
- `seed`: informational; not consumed by the runtime today.
- `users`: array of user specs. Each entry supplies `id`,
  `persona`, optional `hidden_goal`, optional `model`, and an
  optional `initial_action = { tool = "...", args = {...} }`. The
  loader calls `world.spawn_user(...)` with these fields, then
  invokes `user.act(...)` for any `initial_action`.
- `agents`: array of agent specs. `id`, `model`, `tools` get passed
  to `world.spawn_agent`.
- `graders`: a table of name => grader expression. Each expression
  is evaluated against the context the loader builds (see below);
  truthy values become `1.0`, falsy `0.0`.

## Grader expressions

Grader expressions live in a tiny boolean DSL: `and`, `or`, `not`,
parens, and bare names. Calls, attribute access, and literals
other than `true`/`false` are rejected. `ensemble.safe_eval(expr,
ctx)` is the underlying evaluator; the scenario loader builds the
context to include:

- `true`, `false`, `any_event`, `turn_count` (the literal
  shortcuts).
- Every world predicate by name (`had_double_refund`,
  `any_escalation`, etc.) evaluated against the trace.
- Per-user predicates as `<user_id>_<predicate>`, evaluated with
  `args = {"user_id": "<user_id>"}`. A scenario whose users are
  `alice` and `bob` and whose world publishes
  `hidden_goal_resolved` gets `alice_hidden_goal_resolved` and
  `bob_hidden_goal_resolved` in scope automatically.

Unknown names raise rather than silently returning false, so a
typo in a grader expression fails loudly at run time.
