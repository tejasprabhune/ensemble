# Live API integration tests

The tests in this directory hit real Anthropic and OpenAI endpoints
through the ensemble runtime. They cost money. The suite is gated
behind an environment variable so it does not run as part of
`uv run pytest tests/`.

## What they cover

- `test_anthropic_single_agent.py` — one Agora scenario with a
  `claude-haiku-4-5-20251001` agent. Verifies the Anthropic content-block
  shape is parsed correctly and the `lookup_user` tool is routed
  through the world registry.
- `test_openai_single_agent.py` — the same shape against
  `gpt-4o-mini`. Verifies the OpenAI `function_calling` response is
  parsed and dispatched.
- `test_refund_storm_small.py` — a trimmed `refund_storm` (two users,
  one agent, claude-haiku). Verifies multi-actor scheduling, state
  diffs, and the predicate-based grader path against a real model.
- `test_mcp_live_claude.py` — spawns `ensemble mcp serve` with the
  scenario agent slot bound to claude-haiku as a real MCP client.
  The test acts as the external client: it drains alice's seed
  message, asks claude what tool to call, dispatches it via MCP,
  and asserts the trace records both sides of the exchange.

## Running

```
LIVE_API_TESTS=1 \
  ANTHROPIC_API_KEY=... \
  OPENAI_API_KEY=... \
  uv run pytest tests/integration_live/ -v
```

Either key may be omitted; tests that need a key they don't have are
individually skipped (`have_anthropic` / `have_openai` fixtures in
`conftest.py`).

## Cost

Each test issues a single short prompt and at most a handful of
follow-ups; total spend for one full run lands around $0.05 to $0.20
depending on how chatty the models are that day. Treat the suite as
an occasional verification, not a CI hook.

If you want to dial costs down further: shrink the system prompts in
each test file, drop `max_tokens` on the LLM backends (32 is plenty
for a tool-call test), or skip `test_refund_storm_small.py` (the
expensive one).

## CI

The default workflow at `.github/workflows/ci.yml` runs only the
unit and mock-backed integration tests on every push. The live suite
runs on demand via `.github/workflows/live.yml`
(`workflow_dispatch`); the workflow pulls API keys from repo secrets.
