"""Power-user `async with world.simulate()` path with mid-run wait_until."""

import pytest

from ensemble import RunResult, scenario
from ensemble.scenario import _REGISTRY


@pytest.mark.asyncio
async def test_async_with_wait_until_fires_mid_run():
    @scenario("with_smoke")
    async def with_smoke(world):
        for _ in range(8):
            world._mock_say("user-model", "ok")
            world._mock_say("agent-model", "got it")

        alice = world.spawn_user(id="alice", model="user-model")
        world.spawn_agent(id="rep", model="agent-model")
        alice.say("rep", "kick things off")

        async with world.simulate() as run:
            fired = await run.wait_until(world.turn_count >= 3, timeout_ms=5000)
            assert fired, "wait_until should have fired by 3 turns"
            observed = int(world.turn_count)
            assert observed >= 3

        events = world.trace()
        assert len(events) >= 3
        return {"observed_at_3": float(observed)}

    result: RunResult = await _REGISTRY["with_smoke"]("noop")
    assert result.scores["observed_at_3"] >= 3
