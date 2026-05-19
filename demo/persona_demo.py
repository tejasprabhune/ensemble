"""Like hello_world.py, but Alice has a real persona.

The previous demo gave both actors the same model and no system
prompt, so Alice would just echo the agent's question back. Here we
load one of the shipped Plank persona TOMLs through ensemble_train
and pass the rendered system prompt to spawn_user, which keeps Alice
firmly in character for the whole rollout.

Run from the demo/ directory so the .env in this folder is picked up:

    cd demo
    uv run persona_demo.py

If your shell already has OPENAI_API_KEY exported (and it differs
from .env), unset it first or pass dotenv="override" to World.
"""

from pathlib import Path

from ensemble import RunResult, World, run_scenario, scenario
from ensemble_train import load_persona

PERSONA_PATH = (
    Path(__file__).resolve().parent.parent
    / "examples/plank/personas/frustrated_power_user.toml"
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
            "You are a customer support agent for Plank, a small SaaS "
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
    result: RunResult = run_scenario(
        "persona_demo", world_name="noop", backend="openai"
    )
    print(f"scores: {result.scores}")
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
        print(f"  [{event['tick']:>3}] {actor:6} {kind:14} {str(body)[:120]}")
