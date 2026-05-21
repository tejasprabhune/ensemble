from pathlib import Path

from ensemble import RunResult, World, run_scenario, scenario
from ensemble_train import load_persona

PERSONA_PATH = (
    Path(__file__).resolve().parent.parent
    / "examples/agora/personas/frustrated_power_user.toml"
)


@scenario("persona_demo")
async def persona_demo(world: World):
    persona = load_persona(PERSONA_PATH)

    alice = world.spawn_user(
        id="alice",
        persona=persona.name,
        hidden_goal=persona.hidden_state_schema.get("hidden_goal", {}).get("default"),
        model="gpt-5.4",
        system_prompt=persona.system_prompt_template,
    )
    rep = world.spawn_agent(
        id="rep",
        model="gpt-5.4",
        system_prompt=(
            "You are a customer support agent for Agora, a small SaaS "
            "tool. Be concise, helpful, and stick to what policy allows. "
            "Do not promise refunds you cannot deliver."
        ),
    )

    alice.say(
        "rep",
        "i've been paying you for nothing for months. what are you going to do about it?",
    )

    yield world.until(world.turn_count > 8)
    yield {"completed": 1.0}


if __name__ == "__main__":
    import json

    result: RunResult = run_scenario(
        "persona_demo", world_name="noop", backend="openai"
    )
    print(f"scores: {result.scores}")
    print(f"events: {len(result.trace)}")
    for event in result.trace:
        payload = event["payload"]
        kind = payload["kind"]
        actor = event.get("actor") or "-"
        body = payload.get("text") or payload.get("name") or payload.get("note") or ""
        print(f"  [{event['tick']:>3}] {actor:6} {kind:14} {str(body)[:120]}")

    out = Path("traces/persona_demo.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        for event in result.trace:
            f.write(json.dumps(event) + "\n")
    print(f"\nwrote trace to {out.resolve()}")
    print("view it with:")
    print(f"  ensemble trace view {out} --site ../site --port 8765")
