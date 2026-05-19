"""TOML and Python-defined scenarios produce equivalent runs."""

import pytest

from ensemble import load_manifest, scenario
from ensemble.scenario import _REGISTRY


@pytest.mark.asyncio
async def test_toml_and_python_scenarios_match(tmp_path):
    # Python-defined scenario.
    @scenario("parity.py_version")
    async def py_version(world):
        alice = world.spawn_user(
            id="alice",
            persona="patient_retail",
            model="user-model",
        )
        world.spawn_agent(id="rep", model="agent-model", tools=[])
        alice.act("open_ticket", subject="hello")
        yield world.until(world.turn_count >= 1)
        yield {"opened": 1.0}

    # Equivalent TOML scenario.
    manifest = tmp_path / "parity.toml"
    manifest.write_text(
        """
[scenario."parity.toml_version"]
world = "noop"
duration_turns = 0

[[scenario."parity.toml_version".users]]
id = "alice"
persona = "patient_retail"
model = "user-model"
initial_action = { tool = "open_ticket", args = { subject = "hello" } }

[[scenario."parity.toml_version".agents]]
id = "rep"
model = "agent-model"
tools = []

[scenario."parity.toml_version".graders]
opened = "any_event"
"""
    )
    load_manifest(manifest)

    py_result = await _REGISTRY["parity.py_version"]("noop")
    toml_result = await _REGISTRY["parity.toml_version"]("noop")

    py_tool_calls = [
        e for e in py_result.trace if e["payload"]["kind"] == "tool_call"
    ]
    toml_tool_calls = [
        e for e in toml_result.trace if e["payload"]["kind"] == "tool_call"
    ]
    assert len(py_tool_calls) == 1
    assert len(toml_tool_calls) == 1
    assert py_tool_calls[0]["payload"]["name"] == "open_ticket"
    assert toml_tool_calls[0]["payload"]["name"] == "open_ticket"
    assert py_tool_calls[0]["payload"]["args"] == toml_tool_calls[0]["payload"]["args"]

    # Both report a positive grade for the same underlying observation.
    assert py_result.scores["opened"] == toml_result.scores["opened"] == 1.0
