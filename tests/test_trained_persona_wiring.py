"""Trained persona TOMLs route the spawned user to a per-user vLLM backend.

The persona's TOML declares ``mode = "trained"`` and supplies an
``adapter_name``; ``World.spawn_user`` resolves the base URL from
``persona.training.serve_url`` or the ``ENSEMBLE_VLLM_BASE_URL`` env
var and stashes the choice on the user actor. We verify the
resolution without standing up an actual vLLM server by reading the
resolved choice back through ``User.backend_info``.
"""

from __future__ import annotations

import textwrap

import plank  # noqa: F401  registers plank's personas dir
import pytest
from ensemble import World


@pytest.fixture
def trained_persona(tmp_path, monkeypatch):
    """Lay down a minimal trained persona TOML and register a
    throwaway world that points at the directory it lives in."""
    pdir = tmp_path / "personas"
    pdir.mkdir()
    (pdir / "trained_demo.toml").write_text(
        textwrap.dedent(
            """
            [persona]
            name = "trained_demo"
            mode = "trained"
            description = "A trained persona used for wiring tests."

            [persona.hidden_state.schema]
            mood = { type = "string", default = "neutral" }

            [persona.system_prompt]
            template = "You are a trained demo persona."

            [persona.training]
            base_model = "Qwen/Qwen2.5-7B-Instruct"
            adapter_name = "test-org/demo-adapter"
            serve_url = "http://127.0.0.1:9999/v1"
            """
        )
    )
    from ensemble import register_world

    register_world("trained_demo_world", tools=[], predicates=[], personas_dir=pdir)
    yield {"name": "trained_demo_world"}


def test_trained_persona_routes_to_vllm_with_adapter(trained_persona):
    world = World(trained_persona["name"], backend="mock")
    alice = world.spawn_user(id="alice", persona="trained_demo")

    info = alice.backend_info
    assert info is not None, "trained persona should have a per-user backend"
    assert info["kind"] == "vllm"
    assert info["base_url"] == "http://127.0.0.1:9999/v1"
    assert info["adapter"] == "test-org/demo-adapter"


def test_trained_persona_falls_back_to_env_var(tmp_path, monkeypatch):
    pdir = tmp_path / "personas"
    pdir.mkdir()
    (pdir / "env_routed.toml").write_text(
        textwrap.dedent(
            """
            [persona]
            name = "env_routed"
            mode = "trained"

            [persona.training]
            adapter_name = "test-org/env-adapter"
            """
        )
    )
    from ensemble import register_world

    register_world("env_routed_world", tools=[], predicates=[], personas_dir=pdir)
    monkeypatch.setenv("ENSEMBLE_VLLM_BASE_URL", "http://10.0.0.5:8000/v1")

    world = World("env_routed_world", backend="mock", dotenv=False)
    alice = world.spawn_user(id="alice", persona="env_routed")
    info = alice.backend_info
    assert info is not None
    assert info["base_url"] == "http://10.0.0.5:8000/v1"
    assert info["adapter"] == "test-org/env-adapter"


def test_trained_persona_without_serve_url_or_env_falls_back(
    tmp_path, monkeypatch
):
    """When mode=trained but no vLLM endpoint is configured anywhere,
    the user falls back to the world's default backend and a System
    note records the choice so the trace shows what happened."""
    pdir = tmp_path / "personas"
    pdir.mkdir()
    (pdir / "lonely.toml").write_text(
        textwrap.dedent(
            """
            [persona]
            name = "lonely"
            mode = "trained"

            [persona.training]
            adapter_name = "test-org/lonely-adapter"
            """
        )
    )
    from ensemble import register_world

    register_world("lonely_world", tools=[], predicates=[], personas_dir=pdir)
    monkeypatch.delenv("ENSEMBLE_VLLM_BASE_URL", raising=False)

    world = World("lonely_world", backend="mock", dotenv=False)
    alice = world.spawn_user(id="alice", persona="lonely")
    assert alice.backend_info is None
    notes = [
        e["payload"]["note"]
        for e in world.trace()
        if e["payload"]["kind"] == "system"
    ]
    assert any("trained-persona fallback" in n for n in notes)


def test_prompted_persona_keeps_world_backend(trained_persona):
    """Sanity check that a non-trained persona is unaffected: the
    user shares the world's backend, backend_info is None."""
    world = World("plank", backend="mock")
    alice = world.spawn_user(id="alice", persona="patient_retail")
    assert alice.backend_info is None
