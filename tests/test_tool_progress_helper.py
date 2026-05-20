"""tool() injects an emit_progress callable when the function declares it.

A python plugin tool that takes an ``emit_progress`` parameter
receives a callable; each call records one entry that the runtime
flushes to the trace as a ``progress`` event ahead of the trailing
tool result. Tools that don't declare the parameter keep the
existing call shape.
"""

from __future__ import annotations

from ensemble import PluginTool, World, register_world, tool


def test_tool_helper_injects_emit_progress_callable():
    def crunch(steps: int, emit_progress):
        for i in range(1, steps + 1):
            emit_progress(i / steps, f"step {i}/{steps}")
        return {"ok": True, "steps": steps}

    plugin = tool(
        "crunch",
        "Crunch with progress updates.",
        {
            "type": "object",
            "properties": {"steps": {"type": "integer"}},
            "required": ["steps"],
        },
        crunch,
    )

    register_world("crunch_world", tools=[plugin])

    world = World("crunch_world", backend="mock")
    user = world.spawn_user(id="alice", model="user-model")
    user.act("crunch", steps=3)

    trace = world.trace()
    progress = [e for e in trace if e["payload"]["kind"] == "progress"]
    assert len(progress) == 3
    fractions = [p["payload"]["fraction"] for p in progress]
    assert fractions == sorted(fractions)
    assert progress[-1]["payload"]["fraction"] == 1.0
    assert progress[0]["payload"]["message"] == "step 1/3"


def test_tool_helper_without_emit_progress_param_unchanged():
    """A tool that doesn't declare emit_progress should keep working
    exactly as before: the helper threads args through unchanged
    and no progress events get added."""

    def silent(query: str):
        return {"echo": query}

    plugin = tool(
        "silent",
        "Echo the query.",
        {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        silent,
    )

    register_world("silent_world", tools=[plugin])
    world = World("silent_world", backend="mock")
    user = world.spawn_user(id="alice", model="user-model")
    user.act("silent", query="hello")

    trace = world.trace()
    progress = [e for e in trace if e["payload"]["kind"] == "progress"]
    assert progress == []
    results = [e for e in trace if e["payload"]["kind"] == "tool_result"]
    assert results[-1]["payload"]["result"]["echo"] == "hello"
