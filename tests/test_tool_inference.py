"""Bare-and-inferred @tool: name, description, JSON-Schema all come
from the function. Locks in the ergonomic single-line registration
path the audit called out."""

from typing import List, Optional

from ensemble import register_world, tool
from ensemble.world import _TOOL_META_ATTR


def test_bare_tool_infers_name_description_and_schema():
    @tool
    def lookup_user(user_id: str, eager: bool = False) -> dict:
        """Return the user record by id."""
        return {"id": user_id, "eager": eager}

    meta = getattr(lookup_user, _TOOL_META_ATTR)
    assert meta["name"] == "lookup_user"
    assert meta["description"] == "Return the user record by id."
    assert meta["parameters"] == {
        "type": "object",
        "properties": {
            "user_id": {"type": "string"},
            "eager": {"type": "boolean"},
        },
        "required": ["user_id"],
    }


def test_bare_tool_handles_optional_and_list():
    @tool
    def search(query: str, tags: Optional[List[str]] = None) -> list:
        """Search the corpus."""
        return [query]

    meta = getattr(search, _TOOL_META_ATTR)
    assert meta["parameters"]["properties"]["query"] == {"type": "string"}
    assert meta["parameters"]["properties"]["tags"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert meta["parameters"]["required"] == ["query"]


def test_overrides_layer_on_top_of_inference():
    @tool(description="Custom description overrides the docstring.")
    def do_thing(x: int) -> int:
        """This docstring is shadowed."""
        return x

    meta = getattr(do_thing, _TOOL_META_ATTR)
    assert meta["name"] == "do_thing"
    assert meta["description"] == "Custom description overrides the docstring."
    assert meta["parameters"]["properties"]["x"] == {"type": "integer"}


def test_inferred_tool_registers_and_runs_through_register_world():
    @tool
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    register_world("inferred_tool_world", tools=[add])

    from ensemble import World
    w = World("inferred_tool_world", backend="mock", verbose=False)
    assert "add" in w.tool_names()
