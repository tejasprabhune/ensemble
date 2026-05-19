import pytest

from ensemble import Agent, User, World


def test_construct_noop_world():
    w = World("noop")
    assert w.name == "noop"
    assert w.actor_count() == 0


def test_unknown_world_rejected():
    with pytest.raises(ValueError):
        World("does-not-exist")


def test_spawn_user_and_agent():
    w = World("noop")
    alice = w.spawn_user(id="alice", persona="frustrated_power_user", hidden_goal="refund_3mo")
    rep = w.spawn_agent(id="rep1", model="claude-sonnet-4-5", tools=["lookup", "refund"])
    assert isinstance(alice, User)
    assert isinstance(rep, Agent)
    assert alice.id == "alice"
    assert rep.id == "rep1"
    assert w.actor_count() == 2


def test_user_say_queues_message():
    w = World("noop")
    alice = w.spawn_user(id="alice", persona="frustrated_power_user")
    w.spawn_agent(id="rep1", tools=[])
    # Should not raise.
    alice.say("rep1", "I need help with my account")
