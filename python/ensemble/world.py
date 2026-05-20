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

import inspect
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
    timeout_ms: Optional[int] = None
    resources: Optional[List[str]] = None
    # When True, the World wraps `fn` so each call runs in a fresh
    # python subprocess. This is the sandbox path: a tool that
    # imports torch and runs a user-supplied kernel cannot poison the
    # scheduler if it segfaults or leaves the CUDA context in a bad
    # state. The worker reimports the world's python package so the
    # tool is rebuilt from scratch; any state the closure captured in
    # the parent is *not* shared with the worker. Use sandbox=True
    # only for tools whose work is fully encoded in their args.
    sandbox: bool = False
    # When sandbox=True, the world name the worker should import to
    # re-register the tool. Required so the worker knows which
    # plugin package to load. Filled in by `World.__init__` if left
    # blank.
    sandbox_world: Optional[str] = None


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
    # Resources the world declares. Keys are resource names; values
    # are integer permit counts (1 = exclusive, N = shared with N
    # permits). Lazy declarations from ``Tool.with_resources`` still
    # fall back to exclusive semantics; this dict lets the manifest
    # author declare richer shapes ahead of time.
    resources: Dict[str, int] = field(default_factory=dict)

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
    resources: Optional[Dict[str, Any]] = None,
) -> WorldDefinition:
    """Register a world plugin under ``name``. Idempotent: calling
    twice for the same name overwrites the prior definition (so a
    world package can re-register after a hot-reload during dev).

    Worlds with per-instance state pass ``setup``: a zero-arg callable
    returning ``(tools, predicates)`` invoked once per ``World(name)``
    construction. Worlds whose tools are stateless can pass ``tools``
    and ``predicates`` directly.

    ``resources`` declares named permit counts the world wants the
    runtime to enforce. Each value is either an int (the permit count,
    1 for an exclusive lock) or a dict ``{"permits": N}`` matching
    the world.toml schema. Tools that name a resource via
    :meth:`Tool.with_resources` acquire one permit at dispatch time;
    a lazy declaration still falls back to exclusive semantics, so
    populating this dict is only needed for shared resources or for
    making the schema explicit ahead of any tool reference.
    """
    if setup is not None and (tools is not None or predicates is not None):
        raise ValueError("pass either setup= or tools=/predicates=, not both")
    pd = Path(personas_dir).expanduser().resolve() if personas_dir else None
    resolved_resources: Dict[str, int] = {}
    for rname, raw in (resources or {}).items():
        if isinstance(raw, int):
            permits = raw
        elif isinstance(raw, dict) and "permits" in raw:
            permits = int(raw["permits"])
        else:
            raise ValueError(
                f"resource {rname!r}: expected an int permit count or a "
                f"{{permits: N}} dict, got {raw!r}"
            )
        if permits < 1:
            raise ValueError(
                f"resource {rname!r}: permits must be >= 1, got {permits}"
            )
        resolved_resources[rname] = permits
    defn = WorldDefinition(
        name=name,
        setup=setup,
        personas_dir=pd,
        static_tools=list(tools or []),
        static_predicates=list(predicates or []),
        resources=resolved_resources,
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

    The wrapped function may return:

    * a dict, which is sent as the tool effect;
    * a ``(effect, diff)`` tuple, which records a world state diff
      alongside the effect; or
    * a ``ToolReturn`` dict-like with optional ``effect``, ``diff``,
      ``costs``, and ``progress`` keys. The last form is what tools
      need when they want to annotate cost (gpu_seconds, usd, tokens)
      or emit progress entries for long-running work.

    Either ``fn(**args)`` or ``fn(args)`` is tried, in that order. The
    return is JSON-serialized for the native side; cost and progress
    are forwarded into the runtime's cost/progress streams.

    A function that takes an ``emit_progress`` parameter receives a
    callable ``emit_progress(fraction: float, message: str = "")``;
    each call records one entry that the runtime flushes to the trace
    as a ``progress`` event right before the trailing tool result.
    Tools that don't need progress reporting simply omit the
    parameter and the helper threads through the args dict
    unchanged.
    """

    _META_KEYS = {"effect", "diff", "costs", "progress"}
    sig: Optional[inspect.Signature]
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        sig = None
    wants_emitter = (
        sig is not None
        and any(
            p.name == "emit_progress"
            and p.kind
            in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
            for p in sig.parameters.values()
        )
    )

    def wrapped(args_json: str) -> str:
        args = json.loads(args_json) if args_json else {}
        progress_entries: List[Dict[str, Any]] = []

        def emit_progress(fraction: float, message: str = "") -> None:
            progress_entries.append(
                {"fraction": float(fraction), "message": str(message)}
            )

        try:
            if isinstance(args, dict):
                kwargs = dict(args)
                if wants_emitter:
                    kwargs["emit_progress"] = emit_progress
                out = fn(**kwargs)
            else:
                out = fn(args, emit_progress) if wants_emitter else fn(args)
        except TypeError:
            out = fn(args)

        body: Dict[str, Any] = {}
        if isinstance(out, tuple) and len(out) == 2:
            effect, diff = out
            body["effect"] = effect
            if diff is not None:
                body["diff"] = diff
        elif isinstance(out, dict) and _META_KEYS & set(out.keys()):
            if "effect" in out:
                body["effect"] = out["effect"]
            else:
                body["effect"] = {k: v for k, v in out.items() if k not in _META_KEYS} or None
            for key in ("diff", "costs", "progress"):
                if key in out and out[key] is not None:
                    body[key] = out[key]
        else:
            body["effect"] = out

        if progress_entries:
            existing = body.get("progress") or []
            body["progress"] = list(existing) + progress_entries
        return json.dumps(body)

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
