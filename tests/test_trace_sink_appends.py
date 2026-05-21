"""TraceFile sink appends by default; reattaching does not truncate.

A python session that calls ``set_trace_path(path)``, detaches, and
later reattaches to the same path keeps the events written in
between. The CLI's run subcommand is the caller that wants a fresh
file per run; it unlinks the path before invoking the scenario, so
the sink itself never silently discards prior contents.
"""

from __future__ import annotations

import json

import agora  # noqa: F401  registers the world
from ensemble import World


def _count_events(path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in open(path) if line.strip())


def test_reattaching_sink_does_not_truncate(tmp_path):
    sink = tmp_path / "trace.jsonl"

    world1 = World("agora", backend="mock", trace_path=str(sink))
    alice = world1.spawn_user(id="alice", model="user-model")
    alice.act("lookup_user", user_id="u-alice")
    first_count = _count_events(sink)
    assert first_count >= 2, "expected the first session to write events"

    # Detach the sink without unlinking the file; subsequent appends
    # from another session should land at the end of the existing
    # contents, not overwrite them.
    world1.set_trace_path(None)
    assert _count_events(sink) == first_count

    world2 = World("agora", backend="mock", trace_path=str(sink))
    bob = world2.spawn_user(id="bob", model="user-model")
    bob.act("lookup_user", user_id="u-bob")
    final_count = _count_events(sink)
    assert final_count > first_count, (
        f"reattach should have appended; first={first_count}, final={final_count}"
    )
    # The first session's events are still on disk.
    with open(sink) as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert any(
        e["payload"].get("kind") == "tool_call"
        and e["payload"].get("args", {}).get("user_id") == "u-alice"
        for e in lines
    )
    assert any(
        e["payload"].get("kind") == "tool_call"
        and e["payload"].get("args", {}).get("user_id") == "u-bob"
        for e in lines
    )


def test_set_trace_path_none_detaches(tmp_path):
    sink = tmp_path / "drop.jsonl"
    world = World("agora", backend="mock", trace_path=str(sink))
    assert world.trace_path == str(sink)
    world.set_trace_path(None)
    assert world.trace_path is None
