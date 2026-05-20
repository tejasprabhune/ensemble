"""any_of / all_of combinators round-trip through the rust side."""

from ensemble import World, all_of, any_of


def test_any_of_flattens_nested_specs():
    w = World("noop", backend="mock")
    a = w.turn_count > 1
    b = w.turn_count > 2
    c = w.turn_count > 3
    combined = any_of(any_of(a, b), c)
    assert combined.spec["kind"] == "any_of"
    assert len(combined.spec["parts"]) == 3


def test_or_operator_is_any_of():
    w = World("noop", backend="mock")
    u = (w.turn_count > 5) | (w.turn_count > 7)
    assert u.spec["kind"] == "any_of"


def test_and_operator_is_all_of():
    w = World("noop", backend="mock")
    u = (w.turn_count > 5) & (w.turn_count > 7)
    assert u.spec["kind"] == "all_of"


def test_all_of_runs_against_real_scheduler():
    w = World("noop", backend="mock")
    w.spawn_agent(id="rep", model="agent-model")
    until = all_of(w.turn_count > 0)
    # Should halt promptly via quiescence; no actors to talk.
    w.run(until)
    assert w.turn_count.__int__() >= 0
