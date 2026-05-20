"""Resources declared on a world plugin are honoured by the runtime.

`register_world(..., resources={"name": permits})` plumbs through to
the runtime's `ResourceManager`. A Shared resource declared with
``permits = 2`` lets two concurrent tool dispatches that acquire it
proceed in parallel; the same name declared as exclusive (the
default lazy declaration) serializes them.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from ensemble import PluginTool, World, register_world


def _slow_tool(name: str, hold_ms: int, observed: list):
    """Build a plugin tool that records (start, end) timestamps from
    inside the closure. Capturing the timing from inside the tool's
    own execution puts both samples on the same side of the resource
    acquisition, so the test reflects what the runtime's locking
    actually does rather than what the caller saw queued."""

    lock = threading.Lock()

    def run(args_json: str) -> str:
        t_start = time.monotonic()
        time.sleep(hold_ms / 1000.0)
        t_end = time.monotonic()
        with lock:
            observed.append((t_start, t_end))
        return json.dumps({"effect": {"ok": True, "tool": name}})

    return PluginTool(
        name=name,
        description=f"Sleep tool that holds the lane for {hold_ms}ms.",
        parameters={"type": "object", "properties": {}, "required": []},
        fn=run,
        resources=["lane"],
    )


@pytest.mark.asyncio
async def test_shared_resource_lets_concurrent_dispatches_overlap():
    """A Shared{permits: 2} resource should not serialize two
    concurrent dispatches: their executions overlap."""

    observed: list = []
    register_world(
        "shared_lane_world",
        tools=[_slow_tool("hold_a", hold_ms=200, observed=observed)],
        resources={"lane": 2},
    )

    async def attempt(user_id: str):
        world = World("shared_lane_world", backend="mock")
        user = world.spawn_user(id=user_id, model="user-model")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: user.act("hold_a"))

    await asyncio.gather(attempt("u-1"), attempt("u-2"))

    pairs = sorted(observed)
    assert len(pairs) == 2
    assert pairs[1][0] < pairs[0][1], (
        f"shared lane should permit overlap; observed={pairs}"
    )


@pytest.mark.asyncio
async def test_exclusive_resource_serializes_dispatches():
    """An exclusive resource (permits = 1) forces the second dispatch
    to wait for the first to release its permit before starting."""

    observed: list = []
    register_world(
        "exclusive_lane_world",
        tools=[_slow_tool("hold_b", hold_ms=120, observed=observed)],
        resources={"lane": 1},
    )

    async def attempt(user_id: str):
        world = World("exclusive_lane_world", backend="mock")
        user = world.spawn_user(id=user_id, model="user-model")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: user.act("hold_b"))

    await asyncio.gather(attempt("u-1"), attempt("u-2"))

    pairs = sorted(observed)
    assert len(pairs) == 2
    # Tiny tolerance for OS scheduling jitter; the contract is that
    # one dispatch cannot start its work until the previous holder
    # released the lane.
    assert pairs[1][0] >= pairs[0][1] - 5e-3, (
        f"exclusive lane should serialize dispatches; observed={pairs}"
    )


def test_invalid_permit_count_rejected():
    with pytest.raises(ValueError, match="permits must be >= 1"):
        register_world("invalid_world", tools=[], resources={"x": 0})


def test_resource_name_visible_after_declare():
    register_world(
        "visible_resource_world",
        tools=[],
        resources={"lane": 3},
    )
    world = World("visible_resource_world", backend="mock")
    assert "lane" in world._native.resource_names()
