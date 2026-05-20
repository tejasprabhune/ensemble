# Runtime

This page is the reference for the LLM backend layer: the trait
contract, the four implementations that ship in
`ensemble-runtime`, how token and USD cost are recorded against
the calling actor, and the protocol a custom backend implements.

## LLMBackend trait

```rust
// crates/ensemble-runtime/src/backend.rs
#[async_trait]
pub trait LLMBackend: Send + Sync {
    async fn complete(
        &self,
        request: CompletionRequest,
    ) -> Result<CompletionResponse, BackendError>;
}

pub type SharedBackend = Arc<dyn LLMBackend>;
```

A backend is anything that turns a `CompletionRequest` into a
`CompletionResponse`. The trait has exactly one method; the
runtime is otherwise transport- and provider-agnostic. `Arc`
wrapping is what makes the same backend cheaply shareable across
many actors.

## CompletionRequest and CompletionResponse

```rust
pub struct CompletionRequest {
    pub model: String,
    pub system: Option<String>,
    pub messages: Vec<ChatMessage>,
    pub tools: Vec<ToolSchema>,
    pub temperature: Option<f32>,
    pub max_tokens: Option<u32>,
}

pub struct CompletionResponse {
    pub text: String,
    pub tool_calls: Vec<ProposedToolCall>,
    pub stop_reason: Option<String>,
    pub usage: Option<Usage>,
}

pub struct Usage {
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub usd: Option<f64>,
}

pub struct ProposedToolCall {
    pub id: Option<String>,
    pub name: String,
    pub args: serde_json::Value,
}
```

`request.model` is the provider's model identifier (or the mock
backend's script key). `request.system` is the rendered system
prompt; `PromptedPersona` is what fills this in for users with a
persona. `request.messages` is the chat history, `request.tools`
is the agent's tool schemas (already filtered by the per-agent
allowed set; see the [scenarios reference](scenarios.md#spawn_agent)).

`response.text` is the assistant's plain-text content.
`response.tool_calls` is the list of tool invocations the model
returned; the agent loop dispatches them through the world's tool
registry. `response.usage` is populated when the provider returned
a usage block; the runtime turns it into cost annotations.

## Usage and cost annotation

Every shipped backend parses the usage block from its provider's
response. After each completion the agent (or user) actor records
three cost events against itself:

- `tokens_in`: prompt tokens consumed.
- `tokens_out`: completion tokens produced.
- `usd`: the dollar cost, looked up from
  `crates/ensemble-runtime/pricing.toml` by the model id. The
  entry is omitted when the model is not in the table; the runtime
  never fabricates a number.

`pricing.toml` carries per-million-token pricing for the models
the shipped backends use in tests. Update it when a model's
pricing changes; the lookup is exact-match on the model id the
scenario passes to `World(...)`. A custom backend implementer who
wants USD annotation calls `pricing::usd_for(provider, model,
input_tokens, output_tokens)` and stuffs the result into
`Usage.usd` before returning.

Cost annotations land in the world's ledger:
`world.cost_total(unit)` returns the world-wide total,
`world.cost_total(unit, actor=...)` returns the per-actor total,
and `world.set_budget(unit, amount, actor=...)` caps either
ledger. See the [tools reference](tools.md#costs) for the parallel
path tools take to annotate cost from their dispatch return.

## AnthropicBackend

```rust
let be = AnthropicBackend::from_env()?;       // reads ANTHROPIC_API_KEY
let be = AnthropicBackend::with_key("sk-...");
let be = be.with_base_url("https://...");      // for proxies
```

A minimal Anthropic Messages API client. Tool use is via the
official `tools` and `tool_use` content blocks. Streaming is off
deliberately; the framework is non-streaming everywhere.

The base URL defaults to `https://api.anthropic.com/v1`. The
`ANTHROPIC_BASE_URL` env var or `World(base_url=...)` overrides it,
which is the path for Azure AI Foundry or a self-hosted proxy.

The `anthropic-version` header is pinned to `2023-06-01`. Token
usage is parsed from `response.usage.input_tokens` and
`output_tokens`.

## OpenAIBackend

```rust
let be = OpenAIBackend::from_env()?;          // reads OPENAI_API_KEY
let be = OpenAIBackend::with_key("sk-...");
let be = be.with_base_url("https://...");
```

A minimal Chat Completions client with function-calling support.
The base URL defaults to `https://api.openai.com/v1` and is
override-able the same way Anthropic's is. Token usage comes from
`response.usage.prompt_tokens` and `completion_tokens`.

The backend always sends `max_completion_tokens` rather than the
legacy `max_tokens`. See the [max_tokens
divergence](#max_tokens-divergence) section for the rationale.

## LocalAdapterBackend

```rust
let be = LocalAdapterBackend::new("http://localhost:8000/v1");
let be = be.with_adapter("my-adapter");
```

Hits a vLLM-compatible OpenAI-shaped HTTP server. The request
shape is OpenAI's Chat Completions, with the `adapter` field (when
set) forwarded as the `model` field of the request so a single
vLLM server hosting multiple adapters can route by name.

The vLLM backend is what trained personas auto-wire to. The
[personas reference](personas.md#trained-persona-auto-wiring) has
the resolution rules; the short version is: a persona TOML with
`mode = "trained"` and `adapter_name` set picks up `serve_url` (or
the `ENSEMBLE_VLLM_BASE_URL` env var) and constructs a per-user
`LocalAdapterBackend` from those.

Token usage is parsed in OpenAI's shape. USD is looked up against
the OpenAI pricing table; locally-hosted models that aren't on
the public price list don't get a USD entry, which matches the
spirit of the runtime never fabricating a number.

## MockBackend

```rust
let script = MockScript::new();
script.push_for("agent-model", MockTurn::say_then_tool(
    "Looking up.", "lookup_user", json!({"user_id": "u-alice"})
));
script.push_for("agent-model", MockTurn::text("Done."));
let be = MockBackend::new(script);
```

A deterministic scripted backend. Each call pulls the next
`MockTurn` from the script keyed by `request.model`, returning
text and tool calls as the script dictates. When the script runs
out, the mock returns an empty response with `stop_reason =
"script_exhausted"`.

The mock is what the test suite uses everywhere a real provider is
not in play, and what bakes the demo trace served by the front
page. It records no usage, so completions through the mock leave
the cost ledger untouched.

## Backend selection

`World(name, backend=...)` and `run_scenario(..., backend=...)`
accept five strings:

- `"mock"`: the deterministic scripted backend. The default. No
  network.
- `"anthropic"`: Anthropic Messages API. Requires
  `ANTHROPIC_API_KEY`.
- `"openai"`: OpenAI Chat Completions. Requires `OPENAI_API_KEY`.
- `"vllm"`: a self-hosted vLLM endpoint. Requires
  `base_url="..."`.
- `"auto"`: picks the first provider with an API key in the
  environment.

A scenario that wants to point any backend at a different host
passes `base_url="..."` to `World(...)`, which overrides the
provider's default base URL. The env var equivalents
(`ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`) are honoured when the
kwarg is unset.

## Auto-backend precedence

`backend="auto"` resolves in this order:

1. Anthropic, when `ANTHROPIC_API_KEY` is set in the environment
   (or in a `.env` file `World()` auto-loads on construction).
2. OpenAI, when `OPENAI_API_KEY` is set.
3. Mock, when neither key is set.

Anthropic wins ties: a process with both keys gets Anthropic from
`auto`. Pass an explicit `backend="openai"` when the other choice
is intended. The runtime announces the resolved backend on stderr
on construction and appends a `system` event to the trace, so the
final choice is visible without printing it yourself.

## max_tokens divergence

`CompletionRequest.max_tokens` is one field, but the three
shipped real-network backends serialize it differently:

- Anthropic sends `max_tokens` (the Messages API's name).
- OpenAI sends `max_completion_tokens`. Newer chat models
  (`o1`, `gpt-5`, ...) reject the legacy `max_tokens` field, and
  every chat-completions model since mid-2024 accepts the new
  name, so the framework always uses the new field.
- vLLM sends `max_tokens`. vLLM accepts both names; the smaller
  open models people serve through it tend not to track the new
  field, so we stay on the legacy form.

The translation happens inside each backend's `complete`. Scenario
authors only ever interact with the single `max_tokens` field on
`CompletionRequest`.

## Writing a new backend

A custom backend implements `LLMBackend::complete`. The minimal
shape:

```rust
use async_trait::async_trait;
use ensemble_runtime::backend::{
    BackendError, CompletionRequest, CompletionResponse, LLMBackend, Usage,
};

pub struct MyBackend { /* ... */ }

#[async_trait]
impl LLMBackend for MyBackend {
    async fn complete(
        &self,
        request: CompletionRequest,
    ) -> Result<CompletionResponse, BackendError> {
        // ... send the request, parse the response ...
        Ok(CompletionResponse {
            text: text,
            tool_calls: tool_calls,
            stop_reason: stop_reason,
            usage: Some(Usage {
                input_tokens: input_tokens,
                output_tokens: output_tokens,
                usd: None,  // or pricing::usd_for(...) if you have a table
            }),
        })
    }
}
```

Wrap the backend in `Arc::new(MyBackend { ... })` to get a
`SharedBackend`, then thread it into the actors however your
integration calls for. The python `World` constructor builds its
backend internally from the `backend=` string, so a custom backend
needs either a thin pyo3 wrapper or a scenario that constructs
rust actors directly (`tests/integration_live/` is the pattern).

# Traces

See the companion [traces reference](traces.md) for the full
event-log format.
