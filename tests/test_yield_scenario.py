"""Yield-style @scenario decorator end-to-end against the mock backend."""

import pytest

from ensemble import RunResult, run_scenario, scenario


def test_yield_scenario_runs_against_mock(tmp_path):
    @scenario("smoke")
    async def smoke(world):
        # Script two turns of agent dialogue so the conversation actually
        # advances when the scheduler runs.
        world._mock_say("user-model", "thanks for the help")
        world._mock_say("agent-model", "happy to help, anything else?")
        world._mock_say("user-model", "no that's all")
        world._mock_say("agent-model", "great, ticket resolved")

        alice = world.spawn_user(id="alice", persona="frustrated", model="user-model")
        rep = world.spawn_agent(id="rep", model="agent-model", tools=[])
        alice.say("rep", "i need help with my refund")

        yield world.until(world.turn_count > 6)
        yield {"alice_satisfied": 1.0}

    result: RunResult = run_scenario("smoke")
    assert isinstance(result, RunResult)
    assert result.name == "smoke"
    assert result.scores == {"alice_satisfied": 1.0}
    # The mocked back-and-forth produced multiple events in the trace.
    assert len(result.trace) >= 4
    kinds = {e["payload"]["kind"] for e in result.trace}
    assert "user_message" in kinds


def test_act_seeds_a_tool_call():
    @scenario("act_seed")
    async def act_seed(world):
        alice = world.spawn_user(id="alice", persona="frustrated", model="user-model")
        world.spawn_agent(id="rep", model="agent-model", tools=[])
        alice.act("open_ticket", subject="want my money back")
        yield world.until(world.turn_count >= 1)
        yield {}

    result = run_scenario("act_seed")
    tool_calls = [
        e for e in result.trace if e["payload"]["kind"] == "tool_call"
    ]
    assert len(tool_calls) == 1
    assert tool_calls[0]["payload"]["name"] == "open_ticket"
    assert tool_calls[0]["payload"]["args"]["subject"] == "want my money back"


def test_scenario_must_yield_until_first():
    @scenario("bad")
    async def bad(world):
        yield {"oops": 0.0}  # this is wrong: first yield should be Until.

    with pytest.raises(TypeError):
        run_scenario("bad")
