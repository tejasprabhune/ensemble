"""Smallest possible scenario, run against whatever LLM backend the
environment provides.

`backend="auto"` reads ANTHROPIC_API_KEY first, then OPENAI_API_KEY,
and falls back to the deterministic mock. The framework prints which
backend it picked (and records it in the trace as a `system` event)
so you never have to guess.

Drop your key into a `.env` file in the cwd and it will be picked up
automatically; ensemble auto-loads `.env` on World construction.
"""

from ensemble import RunResult, World, run_scenario, scenario


@scenario("hello")
async def hello(world: World):
    alice = world.spawn_user(id="alice", model="gpt-5.5")
    rep = world.spawn_agent(id="rep", model="gpt-5.5")
    alice.say("rep", "hi, can you help me reset my password?")

    yield world.until(world.turn_count > 4)
    yield {"saw_reply": 1.0}


if __name__ == "__main__":
    result: RunResult = run_scenario("hello", world_name="noop", backend="auto")
    print("scores:", result.scores)
    print(f"events: {len(result.trace)}")
    # The first event records which backend actually ran the scenario.
    for event in result.trace[:1]:
        print("note:  ", event["payload"].get("note"))

    print(result.trace)
