"""Unit-style tests for the small primitives the worked examples
build on. Most of phase 1-5 added integration coverage; this fills
gaps where individual public surface was untested."""

from __future__ import annotations

import json

import plank  # noqa: F401
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
    world = World("plank", backend="mock")
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


def test_until_combinators_flatten():
    w = World("noop", backend="mock")
    a = w.turn_count > 1
    b = w.turn_count > 2
    c = w.turn_count > 3
    nested = any_of(any_of(a, b), c)
    assert len(nested.spec["parts"]) == 3
    nested_all = all_of(all_of(a, b), c)
    assert len(nested_all.spec["parts"]) == 3
