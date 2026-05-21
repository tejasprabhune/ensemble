"""Persona TOMLs wire system prompt and hidden state into the user actor."""

import agora  # noqa: F401  registers agora personas
from ensemble import World


def test_persona_loads_system_prompt_and_hidden_state():
    world = World("agora", backend="mock")
    alice = world.spawn_user(
        id="alice",
        persona="frustrated_power_user",
        hidden_goal="refund_3mo",
    )
    # Hidden state comes from the persona TOML's hidden_state.schema
    # defaults; the explicit hidden_goal overrides the default.
    assert alice.hidden_state["hidden_goal"] == "refund_3mo"
    assert alice.hidden_state.get("mood") == "annoyed"
    assert alice.persona is not None
    assert "Agora customer" in alice.persona.system_prompt


def test_missing_persona_name_is_a_no_op():
    world = World("agora", backend="mock")
    user = world.spawn_user(id="bob", persona="this_persona_does_not_exist")
    # No file matched; hidden state stays empty, persona attribute is None.
    assert user.persona is None
    assert user.hidden_state == {}
