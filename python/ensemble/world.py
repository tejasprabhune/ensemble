"""World plugin registry.

A world is a python package that calls :func:`register_world` at
import time, supplying its tools, predicates, and personas directory.
Scenarios pull the world in (``import plank``) before constructing
``World("plank")``; the native side then receives the registered
callables and wires them into the rust tool/predicate registries for
this world instance.

Tools are :class:`PluginTool` descriptors. Each carries a name, a
description, a JSON-schema dict for the parameters, and a callable.
The callable accepts a single JSON string of arguments and must
return a JSON string of either ``{"effect": ...}`` or
``{"effect": ..., "diff": ...}``. The :func:`tool` helper wraps a
plain python function (taking a dict and returning a dict / tuple)
so worlds rarely have to think about the JSON ABI.

Predicates are :class:`PluginPredicate` descriptors. Each carries a
name and a callable accepting ``(trace_json: str, args_json: str)``
and returning a bool. The :func:`predicate` helper handles the JSON
unpacking similarly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .persona import register_personas_dir


@dataclass
class PluginTool:
    name: str
    description: str
    parameters: Dict[str, Any]
    fn: Callable[[str], str]


@dataclass
class PluginPredicate:
    name: str
    fn: Callable[[str, str], bool]


Setup = Callable[[], "tuple[Sequence[PluginTool], Sequence[PluginPredicate]]"]


@dataclass
class WorldDefinition:
    name: str
    setup: Optional[Setup] = None
    personas_dir: Optional[Path] = None
    # Static tool / predicate lists are kept around so callers using
    # the simple register_world(name, tools=[...]) form can introspect
    # without invoking the factory.
    static_tools: List[PluginTool] = field(default_factory=list)
    static_predicates: List[PluginPredicate] = field(default_factory=list)

    def build(self) -> "tuple[List[PluginTool], List[PluginPredicate]]":
        """Materialize tools and predicates for one World instance.
        Worlds that need per-instance state (their own SQLite db, etc.)
        return a fresh batch each time; worlds with static tool sets
        return the pre-registered lists every call."""
        if self.setup is not None:
            tools, preds = self.setup()
            return list(tools), list(preds)
        return list(self.static_tools), list(self.static_predicates)


_WORLDS: Dict[str, WorldDefinition] = {}


def register_world(
    name: str,
    *,
    setup: Optional[Setup] = None,
    tools: Optional[Sequence[PluginTool]] = None,
    predicates: Optional[Sequence[PluginPredicate]] = None,
    personas_dir: Optional[Path | str] = None,
) -> WorldDefinition:
    """Register a world plugin under ``name``. Idempotent: calling
    twice for the same name overwrites the prior definition (so a
    world package can re-register after a hot-reload during dev).

    Worlds with per-instance state pass ``setup``: a zero-arg callable
    returning ``(tools, predicates)`` invoked once per ``World(name)``
    construction. Worlds whose tools are stateless can pass ``tools``
    and ``predicates`` directly.
    """
    if setup is not None and (tools is not None or predicates is not None):
        raise ValueError("pass either setup= or tools=/predicates=, not both")
    pd = Path(personas_dir).expanduser().resolve() if personas_dir else None
    defn = WorldDefinition(
        name=name,
        setup=setup,
        personas_dir=pd,
        static_tools=list(tools or []),
        static_predicates=list(predicates or []),
    )
    _WORLDS[name] = defn
    if pd is not None:
        register_personas_dir(name, pd)
    return defn


def get_world(name: str) -> Optional[WorldDefinition]:
    return _WORLDS.get(name)


def registered_world_names() -> List[str]:
    return sorted(_WORLDS)


# Helpers that wrap a plain python tool/predicate function into the
# JSON-string ABI the native side expects. Most world authors will use
# these instead of writing the JSON dance themselves.


def tool(
    name: str,
    description: str,
    parameters: Dict[str, Any],
    fn: Callable[..., Any],
) -> PluginTool:
    """Wrap a python function as a PluginTool.

    The wrapped function may either:

    * accept a single ``args`` dict and return a dict or a
      ``(effect, diff)`` tuple;
    * accept keyword arguments and return the same.

    Effects and diffs are passed through json.dumps.
    """

    def wrapped(args_json: str) -> str:
        args = json.loads(args_json) if args_json else {}
        try:
            out = fn(**args) if isinstance(args, dict) else fn(args)
        except TypeError:
            # Fallback: function wants the args dict as a single arg.
            out = fn(args)
        if isinstance(out, tuple) and len(out) == 2:
            effect, diff = out
            return json.dumps({"effect": effect, "diff": diff})
        return json.dumps({"effect": out})

    return PluginTool(name=name, description=description, parameters=parameters, fn=wrapped)


def predicate(name: str, fn: Callable[[List[Dict[str, Any]], Dict[str, Any]], bool]) -> PluginPredicate:
    """Wrap a python function as a PluginPredicate.

    The wrapped function receives the deserialised trace (list of
    event dicts) and a deserialised args dict; it returns a bool.
    """

    def wrapped(trace_json: str, args_json: str) -> bool:
        trace = json.loads(trace_json) if trace_json else []
        args = json.loads(args_json) if args_json else {}
        return bool(fn(trace, args))

    return PluginPredicate(name=name, fn=wrapped)
