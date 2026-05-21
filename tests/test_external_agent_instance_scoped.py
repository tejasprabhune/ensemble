"""External-agent registration scopes to the World instance, not the class.

Two scenarios running concurrently in the same process can each
declare their own external-agent slot without one leaking the other.
The previous design patched ``World.spawn_agent`` at the class level,
so a concurrent test that exercised both at once would see the wrong
slot fire.
"""

from __future__ import annotations

import asyncio
import textwrap

import agora  # noqa: F401  registers agora
import pytest
from ensemble import World, scenario


@pytest.mark.asyncio
async def test_concurrent_external_agents_do_not_leak():
    @scenario("ext.concurrent_alpha", world="agora")
    async def alpha(world):
        world.spawn_agent(id="alpha_slot", model="agent-model")
        yield world.until(world.turn_count > 1)
        yield {"ok": 1.0}

    @scenario("ext.concurrent_beta", world="agora")
    async def beta(world):
        world.spawn_agent(id="beta_slot", model="agent-model")
        yield world.until(world.turn_count > 1)
        yield {"ok": 1.0}

    captured = {"alpha": None, "beta": None}

    def capture_alpha(w):
        captured["alpha"] = w

    def capture_beta(w):
        captured["beta"] = w

    from ensemble.scenario import _REGISTRY

    results = await asyncio.gather(
        _REGISTRY["ext.concurrent_alpha"](
            "agora",
            external_agent_id="alpha_slot",
            on_world_constructed=capture_alpha,
        ),
        _REGISTRY["ext.concurrent_beta"](
            "agora",
            external_agent_id="beta_slot",
            on_world_constructed=capture_beta,
        ),
    )
    assert captured["alpha"] is not None and captured["beta"] is not None
    assert captured["alpha"] is not captured["beta"]
    assert captured["alpha"]._external_agent is not None
    assert captured["beta"]._external_agent is not None
    assert captured["alpha"]._external_agent.id == "alpha_slot"
    assert captured["beta"]._external_agent.id == "beta_slot"
    for r in results:
        assert r.scores["ok"] == 1.0


def test_external_agent_id_unused_when_no_match():
    """When the scenario does not spawn the agent the external slot
    names, the World keeps the slot empty and the scenario runs as
    if it had no external agent at all."""

    world = World("noop", backend="mock", external_agent_id="never_spawned")
    world.spawn_agent(id="some_other_id", model="agent-model")
    assert world._external_agent is None
    assert len(world.agents) == 1


def test_spawn_external_agent_routes_through_native_layer():
    world = World("noop", backend="mock", external_agent_id="proxy")
    agent = world.spawn_agent(id="proxy", model="agent-model", tools=["x"])
    assert world._external_agent is agent
    assert world._external_agent_tools == ["x"]
    assert agent.id == "proxy"
