from ensemble import run_scenario, scenario, World


@scenario("hello")
async def hello(world: World):
    alice = world.spawn_user(id="alice", model="user-model")
    rep = world.spawn_agent(id="rep", model="agent-model")
    alice.say("rep", "hi")
    yield world.until(world.turn_count > 4)
    yield {"saw_reply": 1.0}


result = run_scenario("hello", world_name="noop")
print(result.scores)
print(len(result.trace), "events")
