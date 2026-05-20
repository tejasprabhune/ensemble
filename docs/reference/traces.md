# Traces

This page is the reference for the JSONL trace format: every event
kind, its schema, when the runtime emits it, and how to read a
trace from Python or Rust. The trace is the canonical output of a
scenario: anything a grader cares about has to leave a footprint
here.

## Event envelope

Every line of a JSONL trace decodes to one event:

```json
{
  "tick": 7,
  "ts_ms": 1737332112345,
  "actor": "alice",
  "message_id": "msg-...",
  "payload": { "kind": "user_message", "text": "..." }
}
```

Fields:

- `tick`: monotonically increasing counter equal to the number of
  events the bus has appended so far. The watcher uses this to
  evaluate `world.turn_count > N` predicates.
- `ts_ms`: unix epoch milliseconds at append time.
- `actor`: the actor id that produced the event, or `null` for
  system-level events (`world.apply`, scheduler notes).
- `message_id`: present on bus-routed messages, absent on direct
  log appends. Used to correlate request/response pairs across
  the bus.
- `payload`: the tagged-union body; the `kind` discriminator names
  the variant.

The rust type lives in `crates/ensemble-core/src/event.rs`:

```rust
pub struct Event {
    pub tick: Tick,
    pub ts_ms: u128,
    pub actor: Option<ActorId>,
    pub message_id: Option<MessageId>,
    pub payload: EventPayload,
}
```

## EventPayload kinds

Each `kind` value below appears as a `payload.kind` discriminator
in the JSON. Variants new in phase 2 (the `seed` flag) default to
`false` in deserialisation, so older traces round-trip.

### user_message

```json
{"kind": "user_message", "text": "i pay every month for nothing."}
```

A simulated user said something. Emitted by `Bus::send` when a
`UserActor::step` reply lands or when scenario setup seeded a
message via `user.say(target, text)`. The `actor` field names the
user; the `to` field of the underlying envelope (not exposed in
the JSON; the bus drops it after routing) was the agent the user
addressed.

### agent_message

```json
{"kind": "agent_message", "text": "Refunding the most recent cycle now."}
```

An agent actor said something. Same shape as `user_message` with
the roles flipped.

### tool_call

```json
{
  "kind": "tool_call",
  "id": "tc-12ab",
  "name": "issue_refund",
  "args": {"user_id": "u-alice", "amount_cents": 5000, "reason": "..."},
  "seed": false
}
```

Emitted whenever a tool dispatch starts. `id` is the
provider-supplied call id (Anthropic's `tool_use.id`, OpenAI's
`tool_calls[].id`) when available, or a freshly minted id for
seeded actions. `name` is the tool the agent (or seed) chose.
`args` is the JSON the model produced. `seed` is `true` when the
call came from `User.act` or `World.apply` rather than from an
actor's runtime turn; see [seeded events](#seeded-events).

### tool_result

```json
{
  "kind": "tool_result",
  "id": "tc-12ab",
  "name": "issue_refund",
  "result": {"ok": true, "data": {"refund_id": "r-..."}},
  "is_error": false,
  "seed": false
}
```

The dispatch's outcome. `id` matches the originating `tool_call`.
`result` is the tool's `effect` envelope. `is_error` is `true`
when the dispatch raised; the result then carries an `error`
string and the calling agent sees a tool error in its next turn.

### state_diff

```json
{
  "kind": "state_diff",
  "diff": [{
    "table": "refunds",
    "row_id": "r-...",
    "field": "row",
    "old": null,
    "new": {"user_id": "u-alice", "amount_cents": 5000, "reason": "..."}
  }],
  "seed": false
}
```

A structured description of the world state change a tool just
applied. Emitted when a tool returns a `diff` in its envelope, or
when `WorldHandle::apply_and_log` runs on the rust side. Always
follows a `tool_result` with the same `id` (correlated by the
preceding event's id field). The viewer's state-changes panel
reads this directly; predicates that walk state changes pattern-match
on it.

### progress

```json
{
  "kind": "progress",
  "id": "tc-12ab",
  "tool": "slow_billing_check",
  "fraction": 0.4,
  "message": "scanned 2/5 months"
}
```

Buffered while a tool runs, flushed in order ahead of the
trailing `tool_result`. `fraction` is a `0.0..=1.0` estimate of
completion; the viewer renders a progress line per entry.

### tool_timeout

```json
{
  "kind": "tool_timeout",
  "id": "tc-12ab",
  "name": "slow_billing_check",
  "after_ms": 2000
}
```

Emitted when a tool dispatch exceeded its declared timeout. The
calling agent sees a tool error; the scenario continues.

### cost

```json
{
  "kind": "cost",
  "unit": "tokens_in",
  "amount": 1342,
  "running_total": 5612
}
```

A cost annotation. `unit` is an open string (`"usd"`,
`"tokens_in"`, `"tokens_out"`, `"gpu_seconds"`, ...). `amount` is
the increment from this event. `running_total` is the world-wide
total for the unit after this event. The `actor` field at the
envelope level distinguishes per-actor accounting from world-wide
annotations.

### system

```json
{"kind": "system", "note": "scheduler quiescent; halting"}
```

A framework-level note. The scheduler emits one at termination
(describing why), the backend layer emits one on construction
(naming the chosen backend), and the python `_log_grader_scores`
helper emits one with the JSON of the final grader scores so the
trace is self-contained.

## Seeded events

`tool_call`, `tool_result`, and `state_diff` events carry a
`seed: bool` field. It is `true` when the event originated from
scenario setup rather than an actor's runtime turn:

- `User.act(tool, **kwargs)` from a scenario marks every event
  it emits with `seed=true`. The events still carry the user's id
  as the `actor`, because the seed action is the user's, but the
  flag tells consumers the event is not the user's runtime
  reaction.
- `World.apply(tool, **kwargs)` produces a seeded sequence with
  `actor=null`. System-level mutation, no actor in play.
- The `AgentActor` runtime loop, the `UserActor` runtime loop,
  and `dispatch_as` (the MCP external slot) all leave `seed=false`.

Predicates that aggregate "what the agent decided to do" filter
on `seed=false` to avoid double-counting setup mutations. The
trace viewer renders seeded tool lines with a muted left border
and a `[seed]` tag so the visual flow stays clear.

## Reading a trace from Python

The simplest path: each line is one JSON object.

```python
import json

with open("traces/plank_refund_storm.jsonl") as f:
    events = [json.loads(line) for line in f if line.strip()]

tool_calls = [e for e in events if e["payload"]["kind"] == "tool_call"]
non_seed = [c for c in tool_calls if not c["payload"]["seed"]]
```

When the trace is the result of a freshly-run scenario, the
`RunResult.trace` field already carries the parsed events:

```python
from ensemble import run_scenario

result = run_scenario("plank.refund_storm", backend="mock")
result.trace      # already parsed
result.scores     # the grader dict
```

A scenario can also pull the trace mid-run:

```python
events = world.trace()           # snapshot at the moment of the call
```

The list grows as the run continues, so calling `world.trace()`
again later returns more events without rerunning anything.

## Reading a trace from Rust

`Event` is the canonical type. Deserialise with `serde_json`:

```rust
// crates/ensemble-core/src/event.rs
use ensemble_core::event::Event;

let mut events: Vec<Event> = Vec::new();
for line in std::io::BufRead::lines(reader) {
    let line = line?;
    if line.trim().is_empty() {
        continue;
    }
    events.push(serde_json::from_str(&line)?);
}
```

A live `EventLog` snapshot is available from inside the runtime:

```rust
let events: Vec<Event> = log.snapshot().await;
```

The serialised form on disk is the same shape as `Event`'s serde
output, so a snapshot and a re-read JSONL are interchangeable.

## The trace viewer's data model

The viewer (`site/viewer.js`) fetches `trace.jsonl`, parses one
event per line, and renders two views: a three-panel timeline
(actors / tool calls / state changes) and a linear chat feed. The
slider scrubs through events; the viewer polls the file every two
seconds, so a long run appended-to in real time keeps growing in
the browser.

Actor identity is collected from the `actor` field. The viewer
infers each actor's kind (user or agent) by looking at the kinds
of payloads they produced: an actor that emits `tool_call` or
`agent_message` is an agent; one that emits `user_message` first
is a user.

The state-changes panel reads `state_diff` events when present
and falls back to summarising `tool_result` payloads otherwise.
Seeded events are styled with a muted left border so they stay
visually distinct from runtime decisions.

The chat view renders code blocks (fenced triple-backtick),
inline backticks, and bare newlines from the markdown subset the
viewer ships. Tool calls and results show as inline annotations
in the conversation; `[cost]`, `[progress]`, and `[state]` notes
appear as muted lines so an extended run stays readable.
