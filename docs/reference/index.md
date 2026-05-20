# Reference

This section is the reference complement to the tutorial pages on
the site. It answers "what does this thing do, exactly" for every
piece of the public surface. Tutorial pages explain when and why;
these pages explain the contract.

Each page is a single scrollable document. There are no
forward-references that ask you to continue on the next page; if
two pages overlap, the relevant sections cross-link, so following
the link reaches what you need without sequential reading.

## [world-api.md](world-api.md)

How to write a world plugin. `register_world` and its kwargs, the
python plugin path through `PluginTool` and predicates, the
`World.apply` system-level mutation entry point, the rust
`WorldState` trait for worlds with a typed rust core, and the
`world.toml` manifest schema field by field. Reach for this page
when you are extending the framework with a new world or wiring
an existing rust state library into a world plugin.

## [scenarios.md](scenarios.md)

The `@scenario` decorator (both flavours), the `World` instance
the scenario function binds against, every method and property the
scenario calls (`spawn_user`, `spawn_agent`, `until`, `run`,
`simulate`, `apply`, the cost ledger, predicate evaluation), and
the `scenarios.toml` declarative format with its grader-expression
DSL. Start here when you are authoring a new scenario or reading
an existing one closely.

## [tools.md](tools.md)

How to declare a tool a world's agents can call. The `PluginTool`
dataclass, the `tool()` helper for the common case, the
JSON-string envelope (`effect`, `diff`, `costs`, `progress`) the
runtime expects, and the four optional capabilities tools opt
into: resource locks, timeouts, progress emission, and cost
annotations. The last section covers sandboxed dispatch through
`ensemble.tool_worker`.

## [personas.md](personas.md)

The persona TOML schema field by field, the hidden-state
mechanism that travels with a user actor and is rendered into the
system prompt, the `PromptedPersona` wrapper, the
`LocalAdapterBackend`, and the auto-wiring that routes a
`mode = "trained"` persona's user to a vLLM-served adapter via
`serve_url` or `ENSEMBLE_VLLM_BASE_URL`. Reach for this page when
you are writing or tuning a persona, or when you are wiring a
trained adapter from `ensemble-train` back into a scenario.

## [cli.md](cli.md)

Every `ensemble` subcommand and every flag, with type, default,
behavior, side effects, and one example invocation. `init`,
`run`, `trace view`, `worlds {list, add, remove, show}`,
`mcp serve` (in both tools-only and scenario-driving forms), and
`train`. Reach for this page when you are looking up a flag you
forgot or wiring the binary into a script.

## [runtime.md](runtime.md)

The `LLMBackend` trait and the four shipped implementations
(`AnthropicBackend`, `OpenAIBackend`, `LocalAdapterBackend`,
`MockBackend`), how `CompletionResponse.usage` becomes per-actor
cost annotations, the `auto` backend's precedence rules, the
`max_tokens` vs `max_completion_tokens` divergence between
providers, and the minimal contract a custom backend implements.
Reach for this page when you are debugging a real-network run or
writing a new backend.

## [traces.md](traces.md)

The JSONL trace format. Every event kind with its schema, when
the runtime emits it, what the `seed` flag means and which paths
set it, and how to read a trace from Python or Rust. The last
section sketches the trace viewer's data model. Reach for this
page when you are writing a predicate, a grader, or any
downstream consumer of trace data.
