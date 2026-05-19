"""Smallest possible scenario, run against whatever LLM backend the
environment provides.

`backend="auto"` reads ANTHROPIC_API_KEY first, then OPENAI_API_KEY,
and falls back to the deterministic mock. The framework prints which
backend it picked and records it in the trace as a `system` event,
so you never have to guess.

Drop your key into a `.env` file in the cwd and it will be picked up
automatically; ensemble auto-loads `.env` on World construction.

To point the OpenAI client at Azure AI Foundry or another
OpenAI-compatible endpoint, set OPENAI_BASE_URL in `.env`:

    OPENAI_API_KEY=sk-...
    OPENAI_BASE_URL=https://<your-resource>.services.ai.azure.com/openai/v1

ANTHROPIC_BASE_URL is the equivalent override for the Anthropic
client. Or pass `base_url="..."` directly to run_scenario / World.

When something goes wrong on the backend (auth, model not deployed,
quota), the framework appends a `system` event to the trace with the
error message instead of failing silently. Print the full trace if
you only see your seed message and a quiescence note.
"""

from ensemble import RunResult, World, run_scenario, scenario


@scenario("hello")
async def hello(world: World):
    alice = world.spawn_user(id="alice", model="gpt-5.4")
    rep = world.spawn_agent(id="rep", model="gpt-5.4")
    alice.say("rep", "hi, can you help me reset my password?")

    yield world.until(world.turn_count > 4)
    yield {"saw_reply": 1.0}


if __name__ == "__main__":
    result: RunResult = run_scenario("hello", world_name="noop", backend="openai")
    print("scores:", result.scores)
    print(f"events: {len(result.trace)}")
    for event in result.trace:
        payload = event["payload"]
        kind = payload["kind"]
        actor = event.get("actor") or "-"
        body = (
            payload.get("text")
            or payload.get("name")
            or payload.get("note")
            or ""
        )
        print(f"  [{event['tick']:>3}] {actor:6} {kind:14} {str(body)[:100]}")
