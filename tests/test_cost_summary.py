"""world.cost_summary aggregates per-unit costs from the trace.
RunResult carries the same dict so the CLI summary line can show
tokens and USD without the caller walking the trace."""

from ensemble import World, register_world, run_scenario, scenario, tool


@tool
def noop() -> str:
    """Do nothing of interest."""
    return "ok"


def test_cost_summary_returns_dict_when_no_costs_recorded():
    register_world("cost_empty_world", tools=[noop])
    w = World("cost_empty_world", backend="mock", verbose=False)
    assert w.cost_summary() == {}


def test_cost_summary_aggregates_manual_record_cost_calls():
    register_world("cost_manual_world", tools=[noop])
    w = World("cost_manual_world", backend="mock", verbose=False)
    w.record_cost("tokens_in", 100, actor="rep")
    w.record_cost("tokens_in", 50, actor="rep")
    w.record_cost("usd", 0.25, actor="rep")
    summary = w.cost_summary()
    assert summary["tokens_in"] == 150.0
    assert summary["usd"] == 0.25


def test_run_result_carries_cost_summary():
    register_world("cost_runresult_world", tools=[noop])

    @scenario("cost_runresult_smoke")
    async def cost_runresult_smoke(world):
        world.spawn_agent(id="rep", tools=["noop"])
        world.record_cost("usd", 0.01, actor="rep")
        yield world.until(world.turn_count >= 1)
        yield {"ok": 1.0}

    result = run_scenario("cost_runresult_smoke", world_name="cost_runresult_world")
    assert result.costs == {"usd": 0.01}
