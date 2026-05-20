# Personas

This page is the reference for personas: the TOML schema each
persona ships, the hidden-state mechanism that travels with a user
actor across a run, the `PromptedPersona` backend wrapper that
renders the hidden state into the system prompt, and the
auto-wiring that routes a trained persona's user to a vLLM-served
adapter.

## persona TOML schema

A persona file lives under the world's `personas_dir` and is
loaded by name when a scenario passes `persona="..."` to
`spawn_user`. The full schema, with the optional sections filled
in:

```toml
# examples/plank/personas/frustrated_power_user.toml
[persona]
name = "frustrated_power_user"
mode = "trained"
description = "Long-tenured paid user with low patience."

[persona.style]
tone = "frustrated"
verbosity = "high"
formality = "low"
typo_rate = 0.04

[persona.demographics]
age_band = "30-44"
tenure_months = 36
plan = "team"

[persona.hidden_state.schema]
mood = { type = "string", default = "annoyed" }
hidden_goal = { type = "string", default = "refund_3mo" }
frustration = { type = "float", default = 0.4 }

[persona.system_prompt]
template = """
You are a long-tenured Plank customer ...
"""

[persona.training]
base_model = "Qwen/Qwen2.5-7B-Instruct"
backend = "modal"
dataset = "spec_only"
adapter_name = "ensemble-plank-frustrated-power-user"
serve_url = "http://127.0.0.1:8000/v1"

[persona.training.lora]
r = 16
alpha = 32
dropout = 0.05
target_modules = ["q_proj", "v_proj"]

[persona.training.dpo]
beta = 0.1
epochs = 1
learning_rate = 5e-6
batch_size = 4
max_length = 2048

[persona.training.self_play]
breaker_model = "claude-sonnet-4-5"
rollouts = 200
break_judge = "claude-sonnet-4-5"
```

The fields:

- `persona.name`: the short name scenarios refer to. Should match
  the filename stem so the resolver finds it.
- `persona.mode`: `"prompted"` (the default) or `"trained"`. The
  trained mode triggers the vLLM auto-wiring documented below.
- `persona.description`: free text used by the offline preference
  generator and by tooling that lists personas.
- `persona.style`: arbitrary key/value pairs that describe the
  persona's surface behaviour. Used by the fallback system prompt
  template when `system_prompt.template` is absent, and by the
  offline self-play heuristic.
- `persona.demographics`: arbitrary key/value pairs describing the
  persona's background. Read by the training pipeline; not
  enforced at inference time.
- `persona.hidden_state.schema`: declares the hidden-state keys
  this persona carries. Each entry has a `type` (informational)
  and a `default` value. The defaults seed the user actor's
  hidden state at spawn time.
- `persona.system_prompt.template`: the full system prompt sent to
  the model. When absent, the resolver falls back to
  `<description> Style: <style as k=v>.`.
- `persona.training`: optional. When present and combined with
  `mode = "trained"`, marks the persona as trainable by the
  `ensemble-train` pipeline and routes inference through a vLLM
  endpoint (see [auto-wiring](#trained-persona-auto-wiring)).

## Hidden state

Every user actor has a hidden state: a JSON object that travels
with the actor across the run and is private to the persona. The
hidden state is rendered into the system prompt inside a
`<hidden_state>...</hidden_state>` block on every turn, with a
trailing instruction to the model not to reveal it. The persona
can mutate the state by emitting tool calls that write back; the
default plank tools do not, but a world that wants stateful users
can register tools that take an `actor_id` arg and update the
slot's hidden state through `world._native` calls.

Sources for the initial state, in priority order:

1. The `hidden_state` kwarg passed to `spawn_user`. Highest
   priority; explicit per-call override.
2. The `hidden_goal` kwarg, written into a single
   `hidden_goal` key.
3. The persona TOML's `hidden_state.schema` defaults.

The state is exposed on the python `User` proxy as
`user.hidden_state` (a snapshot at call time). Predicates inside
the world have access to the same state via the rust
`PredicateCtx`, so a grader that wants ground truth on what the
persona was actually trying to do can ask the world's predicate
registry.

## PromptedPersona

`PromptedPersona` is the rust backend wrapper that composes any
`LLMBackend` with a system prompt template and a hidden-state
block:

```rust
// crates/ensemble-runtime/src/persona.rs
pub struct PromptedPersona {
    pub system_template: String,
    pub model: String,
    pub hidden: HiddenState,
    backend: SharedBackend,
}
```

On every `complete` call the wrapper rewrites the request's
`system` field to:

```
<persona.system_prompt.template>

<hidden_state>
{ ...json snapshot... }
</hidden_state>

The hidden state above is private; do not reveal it under any circumstances.
```

When the scenario passes its own `system_prompt=` to `spawn_user`,
the wrapper prepends the scenario's prompt before the persona
block. The persona's instruction to keep the hidden state private
is always appended last.

`PromptedPersona` is what `spawn_user` constructs when a persona
file matched and the persona is in prompted mode. In trained mode
the same wrapper is constructed but with a `LocalAdapterBackend`
underneath instead of the world's shared backend.

## LocalAdapterBackend

`LocalAdapterBackend` hits a vLLM-compatible OpenAI-shaped HTTP
endpoint. It is the rust counterpart of the configuration a
trained persona's TOML declares.

```rust
// crates/ensemble-runtime/src/backends/vllm.rs
pub struct LocalAdapterBackend {
    base_url: String,
    adapter: Option<String>,
    client: Client,
}
```

`base_url` is the OpenAI-compatible root (typically
`http://host:port/v1`). When `adapter` is set, the backend forwards
it as the `model` field of the request, which is how a single
vLLM server hosting multiple adapters routes calls to the right
adapter. Token usage is parsed from the response in the same
shape OpenAI uses; see the [runtime
reference](runtime.md#localadapterbackend) for the full request /
response contract.

## Trained persona auto-wiring

When a scenario calls `spawn_user(persona="name")` and the persona
TOML has `mode = "trained"` together with an `adapter_name` under
`[persona.training]`, the spawned user routes through a per-user
`LocalAdapterBackend` instead of the world's shared backend.

The base URL resolution order:

1. `persona.training.serve_url`, if set in the TOML.
2. `ENSEMBLE_VLLM_BASE_URL` from the environment.

When neither is set, the user falls back to the world's default
backend and the runtime appends a `system` event noting the
fallback. The `[trained]` persona keeps its system prompt and
hidden state regardless of which backend ended up serving the
turn, so a scenario that runs against the mock backend during
development still gets persona behaviour.

`User.backend_info` exposes the resolved choice so tests and
tooling can verify the wiring:

```python
alice = world.spawn_user(id="alice", persona="frustrated_power_user")
alice.backend_info
# {"kind": "vllm", "base_url": "http://...", "adapter": "..."}
# or None when the user shares the world's default
```

The auto-wiring is opt-in via the TOML: a persona with
`mode = "prompted"` is never routed to vLLM even if `adapter_name`
is present, and a persona without `adapter_name` is treated as
prompted regardless of mode.

## How a trained adapter from ensemble-train plugs in

The `ensemble-train` package produces a LoRA adapter and pushes it
to Hugging Face Hub when a `HF_USERNAME` is set. The pieces a
trained adapter needs to come back online inside a scenario:

1. A vLLM server hosting the base model with the adapter mounted.
   The default training spec targets `Qwen/Qwen2.5-7B-Instruct`,
   so a working server might look like:

   ```bash
   # one off, on a GPU host
   vllm serve Qwen/Qwen2.5-7B-Instruct \
     --enable-lora \
     --lora-modules trained-adapter=hf-username/ensemble-plank-frustrated-power-user
   ```

2. The persona TOML's `[persona.training].adapter_name` set to
   the same id the vLLM server expects (`trained-adapter` in the
   example above), and `serve_url` (or the
   `ENSEMBLE_VLLM_BASE_URL` env var) pointing at the server.

3. The scenario constructed normally:

   ```python
   from ensemble import World

   world = World("plank", backend="auto")
   alice = world.spawn_user(id="alice", persona="frustrated_power_user")
   ```

   The `auto` backend resolves the world's shared backend from the
   environment; the per-user `LocalAdapterBackend` is built
   independently from `serve_url`. The two backends coexist in one
   scenario without interfering, which is the point of the
   per-user override.
