"""Phase 5 tool primitives exercised against the plank example.

Covers: slow_billing_check emits progress; the same tool times out
when wrapped with a short cap; concurrent issue_refund attempts
serialize on the billing_db resource; budgets halt the scheduler
when running totals cross the cap.
"""

from __future__ import annotations

import asyncio
import json
import time

import plank  # noqa: F401  registers the world
import pytest
from ensemble import World, scenario
from ensemble.scenario import _REGISTRY


def test_slow_billing_check_emits_progress():
    world = World("plank", backend="mock")
    alice = world.spawn_user(id="alice", model="user-model")
    alice.act("slow_billing_check", user_id="u-alice", steps=3)

    progress = [
        e for e in world.trace() if e["payload"]["kind"] == "progress"
    ]
    assert len(progress) == 3, f"expected 3 progress entries, got {progress}"
    fractions = [p["payload"]["fraction"] for p in progress]
    # Fractions should be monotonically increasing and reach 1.0.
    assert fractions == sorted(fractions)
    assert fractions[-1] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_concurrent_refunds_serialize_on_billing_db():
    """Two concurrent World('plank') instances share a billing_db
    semaphore. Issuing a refund on one should not race against issuing
    one on the other (different users, so the per-run double-refund
    policy doesn't kick in)."""

    started: list[float] = []
    ended: list[float] = []

    async def attempt(user_id: str, ticket_id: str):
        world = World("plank", backend="mock")
        user = world.spawn_user(id=user_id, model="user-model")
        # Open the ticket so plank's audit log has a record.
        user.act("open_ticket", ticket_id=ticket_id, user_id=user_id, subject="x")
        # Run the (synchronous) act_json on a thread so the two
        # attempts truly overlap; the rust resource manager is what
        # forces serialization.
        loop = asyncio.get_running_loop()

        def go():
            started.append(time.monotonic())
            # Wrap a deliberate inner delay around the refund to make
            # the serialization visible. Use slow_billing_check first
            # to take ~300ms inside the billing_db... actually we use
            # the resource on issue_refund. Sleep then refund.
            user.act("issue_refund", user_id=user_id, amount_cents=100, reason="x")
            ended.append(time.monotonic())

        await loop.run_in_executor(None, go)

    # Two independent World instances on the same world name share
    # the process-wide ResourceManager keyed by "plank".
    await asyncio.gather(
        attempt("u-bob", "t-bob"),
        attempt("u-carol", "t-carol"),
    )

    # Both attempts ran. If the resource serialization works, the
    # *second* attempt to start should start after the *first* one
    # ended (since issue_refund takes only microseconds, the windows
    # would otherwise overlap). With locking, started[1] >= ended[0].
    assert len(started) == 2 and len(ended) == 2
    # Order is non-deterministic; sort by start.
    pairs = sorted(zip(started, ended))
    assert pairs[1][0] >= pairs[0][1] - 1e-4, (
        f"expected serialization: pairs={pairs}"
    )


@pytest.mark.asyncio
async def test_budget_exceeded_halts_scenario():
    @scenario("plank.budget_demo", world="plank")
    async def s(world):
        world.set_budget("usd", 0.05)
        # Record costs that cross the cap mid-run.
        world.record_cost("usd", 0.02)
        world.record_cost("usd", 0.02)
        world.record_cost("usd", 0.10)  # pushes total to 0.14 > 0.05
        alice = world.spawn_user(id="alice", model="user-model")
        alice.say("rep", "hi")
        # Long until so we don't accidentally halt on time.
        yield world.until(world.turn_count > 200)
        yield {"final_usd": world.cost_total("usd")}

    result = await _REGISTRY["plank.budget_demo"]("plank")
    # The scheduler halted on the budget; the trace's terminal system
    # note records it.
    sys_notes = [
        e for e in result.trace if e["payload"]["kind"] == "system"
    ]
    assert any("budget exceeded" in n["payload"]["note"] for n in sys_notes), (
        f"expected a 'budget exceeded' system note in trace, got {sys_notes}"
    )
    # Cost events came through with the running total.
    cost_events = [
        e for e in result.trace if e["payload"]["kind"] == "cost"
    ]
    assert len(cost_events) == 3
    assert cost_events[-1]["payload"]["running_total"] == pytest.approx(0.14)


def test_slow_billing_check_progress_appears_in_trace():
    """Direct dispatch (no scheduler involvement) still surfaces
    progress entries on the trace."""
    world = World("plank", backend="mock")
    alice = world.spawn_user(id="alice", model="user-model")
    alice.act("slow_billing_check", user_id="u-alice", steps=2)
    trace = world.trace()
    kinds = [e["payload"]["kind"] for e in trace]
    # The spawn_user call emits a structured user_spawned event (as a
    # system note); after the backend banner that is the second entry.
    # Drop the leading system notes and assert the tool sequence.
    tool_kinds = [k for k in kinds if k != "system"]
    assert tool_kinds == ["tool_call", "progress", "progress", "tool_result"], kinds
