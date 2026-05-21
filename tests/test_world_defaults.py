"""Per-world default models and auto-generated actor ids."""

from ensemble import World, register_world


def test_register_world_carries_default_models():
    register_world(
        "defaults_world",
        tools=[],
        default_user_model="mini-user-v1",
        default_agent_model="mini-agent-v1",
    )
    w = World("defaults_world", backend="mock", verbose=False)
    u = w.spawn_user()
    a = w.spawn_agent()
    assert u.id == "user-1"
    assert a.id == "agent-1"

    # Sanity: the events the trace consumers see record the resolved
    # model so a researcher inspecting the trace knows what backend
    # the actor was bound to.
    events = w.trace()
    spawned_users = [
        e for e in events
        if e.get("payload", {}).get("kind") == "system"
        and "user_spawned" in e.get("payload", {}).get("note", "")
    ]
    spawned_agents = [
        e for e in events
        if e.get("payload", {}).get("kind") == "system"
        and "agent_spawned" in e.get("payload", {}).get("note", "")
    ]
    assert any("mini-user-v1" in e["payload"]["note"] for e in spawned_users)
    assert any("mini-agent-v1" in e["payload"]["note"] for e in spawned_agents)


def test_explicit_model_overrides_world_default():
    register_world(
        "override_world",
        tools=[],
        default_agent_model="mini-agent-v1",
    )
    w = World("override_world", backend="mock", verbose=False)
    a = w.spawn_agent(model="custom-agent")
    events = w.trace()
    spawned = [
        e for e in events
        if e.get("payload", {}).get("kind") == "system"
        and "agent_spawned" in e.get("payload", {}).get("note", "")
    ]
    assert any("custom-agent" in e["payload"]["note"] for e in spawned)


def test_auto_ids_increment():
    register_world("auto_ids_world", tools=[])
    w = World("auto_ids_world", backend="mock", verbose=False)
    u1 = w.spawn_user()
    u2 = w.spawn_user()
    a1 = w.spawn_agent()
    a2 = w.spawn_agent()
    assert u1.id == "user-1"
    assert u2.id == "user-2"
    assert a1.id == "agent-1"
    assert a2.id == "agent-2"
