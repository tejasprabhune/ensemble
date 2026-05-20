"""Integration tests for the world plugin path.

These verify that a world defined entirely from python (via
register_world, no rust crate) plumbs through to the rust runtime:
agent tool calls dispatch into the python callable, predicates
evaluate against the trace, and World rejects unknown world names.
"""

from __future__ import annotations

import json

import pytest

from ensemble import (
    PluginPredicate,
    PluginTool,
    World,
    register_world,
    scenario,
)
from ensemble.scenario import _REGISTRY


@pytest.fixture
def counter_world():
    """Register a tiny python-only world. Each World instance gets a
    fresh setup() invocation, so per-test state isolation is clean."""

    name = "test_counter"

    def setup():
        state = {"n": 0}

        def increment(args_json):
            data = json.loads(args_json) if args_json else {}
            by = int(data.get("by", 1))
            state["n"] += by
            return json.dumps({
                "effect": {"value": state["n"]},
                "diff": [{
                    "table": "counter",
                    "row_id": "default",
                    "field": "value",
                    "old": state["n"] - by,
                    "new": state["n"],
                }],
            })

        def saw_increment(trace_json, args_json):
            trace = json.loads(trace_json) if trace_json else []
            return any(
                e.get("payload", {}).get("kind") == "tool_call"
                and e["payload"]["name"] == "increment"
                for e in trace
            )

        tools = [PluginTool(
            name="increment",
            description="Add `by` to the counter.",
            parameters={
                "type": "object",
                "properties": {"by": {"type": "integer"}},
                "required": [],
            },
            fn=increment,
        )]
        predicates = [PluginPredicate(name="saw_increment", fn=saw_increment)]
        return tools, predicates

    register_world(name, setup=setup)
    yield name


def test_unknown_world_raises_clear_error():
    with pytest.raises(ValueError, match="no world named"):
        World("this_world_was_never_registered")


def test_plugin_tool_dispatches_through_runtime(counter_world):
    """Agent issues a scripted increment tool call; verify the trace
    contains the tool result, the StateDiff, and the predicate sees
    the call."""

    @scenario("plugin.counter_inc", world=counter_world)
    async def s(world):
        world._native._mock_say_then_tool(
            "agent-model",
            "Bumping the counter.",
            "increment",
            '{"by": 3}',
        )
        world._mock_say("agent-model", "Done.")
        alice = world.spawn_user(id="alice", model="user-model")
        world.spawn_agent(id="rep", model="agent-model", tools=["increment"])
        alice.say("rep", "increment please")
        yield world.until(world.turn_count > 6)
        yield {"saw": 1.0 if world.evaluate_predicate("saw_increment") else 0.0}

    result = await_run("plugin.counter_inc", counter_world)
    assert result.scores["saw"] == 1.0

    tool_results = [e for e in result.trace if e["payload"]["kind"] == "tool_result"]
    assert any(
        tr["payload"]["name"] == "increment"
        and tr["payload"]["result"]["value"] == 3
        for tr in tool_results
    )

    diffs = [e for e in result.trace if e["payload"]["kind"] == "state_diff"]
    assert diffs and diffs[0]["payload"]["diff"][0]["table"] == "counter"


def test_per_world_state_is_isolated(counter_world):
    """Two consecutive World instances should get independent state."""

    @scenario("plugin.counter_iso", world=counter_world)
    async def s(world):
        world._native._mock_say_then_tool(
            "agent-model", "go", "increment", '{"by": 5}'
        )
        world._mock_say("agent-model", "ok")
        alice = world.spawn_user(id="alice", model="user-model")
        world.spawn_agent(id="rep", model="agent-model", tools=["increment"])
        alice.say("rep", "go")
        yield world.until(world.turn_count > 4)
        yield {"value": _last_value(world)}

    r1 = await_run("plugin.counter_iso", counter_world)
    r2 = await_run("plugin.counter_iso", counter_world)
    # Both runs see value=5 because each World rebuilt fresh state.
    assert r1.scores["value"] == 5
    assert r2.scores["value"] == 5


def _last_value(world):
    for e in reversed(world.trace()):
        if (
            e["payload"]["kind"] == "tool_result"
            and e["payload"]["name"] == "increment"
        ):
            return e["payload"]["result"]["value"]
    return None


def test_manifest_scenario_without_world_raises(tmp_path):
    from ensemble import load_manifest

    p = tmp_path / "scenarios.toml"
    p.write_text(
        """
[scenario.no_world]
duration_turns = 4
"""
    )
    with pytest.raises(ValueError, match="'world' field is required"):
        load_manifest(p)


def await_run(name, world_name):
    import asyncio

    return asyncio.run(_REGISTRY[name](world_name))
