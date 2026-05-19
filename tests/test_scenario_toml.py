"""TOML scenarios produce the same kinds of traces and pass grader expressions."""

import pytest

from ensemble import load_manifest, safe_eval
from ensemble.scenario import _REGISTRY


def test_safe_eval_basic():
    ctx = {
        "a": True,
        "b": False,
        "c": True,
        "true": True,
        "false": False,
    }
    assert safe_eval("a", ctx) is True
    assert safe_eval("not b", ctx) is True
    assert safe_eval("a and c", ctx) is True
    assert safe_eval("a and b", ctx) is False
    assert safe_eval("a or b", ctx) is True
    assert safe_eval("(a and b) or c", ctx) is True
    assert safe_eval("not (a and c)", ctx) is False


def test_safe_eval_rejects_unknown_names():
    with pytest.raises(KeyError):
        safe_eval("foo", {"a": True})


def test_safe_eval_rejects_calls():
    with pytest.raises(ValueError):
        safe_eval("foo()", {"foo": True})


@pytest.mark.asyncio
async def test_load_manifest_registers_scenarios(tmp_path):
    manifest = tmp_path / "scenarios.toml"
    manifest.write_text(
        """
[scenario.smoke_toml]
world = "noop"
duration_turns = 4

[[scenario.smoke_toml.users]]
id = "alice"
persona = "patient_retail"
model = "user-model"
initial_action = { tool = "open_ticket", args = { subject = "demo" } }

[[scenario.smoke_toml.agents]]
id = "rep"
model = "agent-model"
tools = []

[scenario.smoke_toml.graders]
saw_event = "any_event"
no_double_refunds = "not had_double_refund"
"""
    )
    load_manifest(manifest)
    assert "smoke_toml" in _REGISTRY
    result = await _REGISTRY["smoke_toml"]("noop")
    assert result.scores["no_double_refunds"] == 1.0
    assert result.scores["saw_event"] == 1.0
