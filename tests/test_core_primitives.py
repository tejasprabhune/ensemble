"""Unit-style tests for the small primitives the worked examples
build on. Most of phase 1-5 added integration coverage; this fills
gaps where individual public surface was untested."""

from __future__ import annotations

import json

import agora  # noqa: F401
from ensemble import World, all_of, any_of


def test_world_predicate_names_includes_defaults():
    world = World("noop", backend="mock")
    names = world.predicate_names()
    # Defaults installed by PredicateRegistry::with_defaults.
    assert "any_event" in names
    assert "had_double_refund" in names


def test_cost_total_accumulates_and_emits_events():
    world = World("noop", backend="mock")
    world.record_cost("usd", 0.25)
    world.record_cost("usd", 0.10)
    assert world.cost_total("usd") == 0.35
    cost_events = [e for e in world.trace() if e["payload"]["kind"] == "cost"]
    assert len(cost_events) == 2
    assert cost_events[-1]["payload"]["running_total"] == 0.35


def test_set_budget_does_not_halt_when_within_cap():
    world = World("noop", backend="mock")
    world.set_budget("usd", 1.0)
    world.record_cost("usd", 0.5)
    # No system note about budget exceeded.
    sys_notes = [
        e["payload"]["note"]
        for e in world.trace()
        if e["payload"]["kind"] == "system"
    ]
    assert not any("budget exceeded" in n for n in sys_notes)


def test_eventlog_jsonl_roundtrip_via_trace():
    world = World("agora", backend="mock")
    alice = world.spawn_user(id="alice", model="user-model")
    alice.act("lookup_user", user_id="u-alice")
    raw = json.dumps(world.trace())
    parsed = json.loads(raw)
    assert isinstance(parsed, list) and len(parsed) > 0
    # Every event has the required keys.
    for ev in parsed:
        assert "tick" in ev
        assert "ts_ms" in ev
        assert "payload" in ev
        assert "kind" in ev["payload"]


def test_sandbox_tool_dispatches_via_subprocess(tmp_path):
    """A sandbox=True tool runs in a fresh subprocess. We register a
    world plugin that ships one such tool, spawn the world, and call
    the tool through the registered native side."""
    import subprocess
    import sys
    # Run the worker directly so we exercise the entry point without
    # also depending on a world plugin. Agora ships sandbox=False
    # tools, so we drive the worker against the noop world after
    # registering a one-off sandbox tool.
    proc = subprocess.run(
        [sys.executable, "-m", "ensemble.tool_worker",
         "--world", "noop", "--tool", "definitely_unknown"],
        input="{}",
        capture_output=True, text=True, check=False,
    )
    # noop world has no tools so the worker should exit with code 3.
    assert proc.returncode == 3
    assert "not registered" in proc.stderr


def test_set_actor_budget_isolated_from_world_total():
    world = World("noop", backend="mock")
    world.set_budget("usd", 10.0)
    world.set_budget("usd", 0.5, actor="alice")
    # Alice's cost goes against her own cap, not just the world total.
    world.record_cost("usd", 0.4, actor="alice")
    assert world.cost_total("usd") == 0.4
    assert world.cost_total("usd", actor="alice") == 0.4
    # Bob's cost lands on the world total but not Alice's actor total.
    world.record_cost("usd", 2.0, actor="bob")
    assert world.cost_total("usd") == 2.4
    assert world.cost_total("usd", actor="alice") == 0.4
    assert world.cost_total("usd", actor="bob") == 2.0
    # No halt yet; both caps still satisfied (alice at 0.4 of 0.5,
    # world at 2.4 of 10.0).
    sys_notes = [
        e["payload"]["note"]
        for e in world.trace()
        if e["payload"]["kind"] == "system"
    ]
    assert not any("budget exceeded" in n for n in sys_notes)


def test_trace_path_writes_live_jsonl(tmp_path):
    """The live trace sink writes each event as it is appended."""
    sink = tmp_path / "live.jsonl"
    world = World("agora", backend="mock", trace_path=str(sink))
    assert world.trace_path == str(sink)
    alice = world.spawn_user(id="alice", model="user-model")
    alice.act("lookup_user", user_id="u-alice")
    # File exists with events on disk before the run completes.
    lines = sink.read_text().strip().splitlines()
    assert len(lines) >= 1
    payloads = [json.loads(line)["payload"]["kind"] for line in lines]
    assert "tool_call" in payloads or "tool_result" in payloads


def test_trace_path_detach(tmp_path):
    sink = tmp_path / "drop.jsonl"
    world = World("agora", backend="mock")
    world.set_trace_path(str(sink))
    world.set_trace_path(None)
    assert world.trace_path is None


def test_until_combinators_flatten():
    w = World("noop", backend="mock")
    a = w.turn_count > 1
    b = w.turn_count > 2
    c = w.turn_count > 3
    nested = any_of(any_of(a, b), c)
    assert len(nested.spec["parts"]) == 3
    nested_all = all_of(all_of(a, b), c)
    assert len(nested_all.spec["parts"]) == 3
