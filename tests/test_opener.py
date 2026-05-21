"""world.opener as a first-class kickoff actor for silent-loop
scenarios, and until_agent_emits / until_done as the matching
stop primitives."""

import pytest

from ensemble import World, register_world, run_scenario, scenario, tool


@tool
def echo(text: str) -> str:
    """Return the text back."""
    return text


def test_opener_emits_kickoff_event_and_seeds_agent_inbox():
    register_world("opener_world", tools=[echo])
    w = World("opener_world", backend="mock", verbose=False)
    agent = w.spawn_agent(id="rep", tools=["echo"])
    opener = w.opener("hello rep", to=agent.id)

    assert opener.id == "opener-1"
    assert len(w.users) == 0
    assert agent in w.agents

    events = w.trace()
    kickoffs = [
        e for e in events
        if e.get("payload", {}).get("kind") == "system"
        and "\"kickoff\"" in e.get("payload", {}).get("note", "")
    ]
    assert len(kickoffs) == 1


def test_until_done_fires_when_agent_emits_done_signal():
    register_world("done_signal_world", tools=[echo])

    @scenario("done_signal")
    async def done_signal(world):
        # Script one agent reply that includes the DONE sentinel so
        # the until_done condition fires on the first turn.
        world._mock_say("done-agent", "doing the work now, DONE")

        agent = world.spawn_agent(id="rep", model="done-agent", tools=["echo"])
        world.opener("get to work", to=agent.id)

        yield world.until_done("rep")
        yield {"finished": 1.0}

    result = run_scenario("done_signal", world_name="done_signal_world")
    assert result.scores == {"finished": 1.0}


def test_until_agent_emits_matches_substring():
    register_world("substring_world", tools=[echo])

    @scenario("substring_emit")
    async def substring_emit(world):
        world._mock_say("rep-model", "kickoff received, beginning the work")
        world.spawn_agent(id="rep", model="rep-model", tools=["echo"])
        world.opener("start", to="rep")
        yield world.until_agent_emits("rep", contains="beginning the work")
        yield {"matched": 1.0}

    result = run_scenario("substring_emit", world_name="substring_world")
    assert result.scores == {"matched": 1.0}


def test_until_agent_emits_rejects_zero_or_multiple_criteria():
    register_world("criteria_world", tools=[echo])
    w = World("criteria_world", backend="mock", verbose=False)
    with pytest.raises(TypeError):
        w.until_agent_emits("rep")
    with pytest.raises(TypeError):
        w.until_agent_emits("rep", contains="a", equals="b")
