"""World plugin registry.

A world is registered in one of two ways. The factory form,
``register_world(name, setup=...)``, builds a ``WorldDefinition``
the ``World(name)`` constructor pulls tools and predicates from.
The subclass form, ``class MyWorld(World)``, attaches tools and
predicates as decorated methods on the subclass and uses
``self`` for per-instance state; the subclass auto-registers
itself when first instantiated.

Both produce the same trace events, the same JSON-string ABI to
the rust core, and the same sandbox semantics. The subclass form
is the idiomatic path when the world has mutable per-instance
state; the factory form is the natural fit when state already
lives in a typed container (a rust core, a sqlite handle).

Tools and predicates use a dual-mode helper: ``tool(...)`` and
``predicate(...)`` work as decorators when given metadata
(``@tool(name="x", description="y", parameters={...})``) and as
factories when given a callable last argument
(``tool("x", "y", schema, fn)``). A decorated method on a
``World`` subclass is marked with ``_ensemble_tool_meta`` so the
subclass walker finds it and builds a bound ``PluginTool`` at
instance-construction time.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from .persona import register_personas_dir


_TOOL_META_ATTR = "_ensemble_tool_meta"
_PREDICATE_META_ATTR = "_ensemble_predicate_meta"


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
    # only for tools whose work is fully encoded in their args, or
    # use ``world.shared_state`` for JSON-serialisable state the
    # runtime forwards across the boundary.
    sandbox: bool = False
    # When sandbox=True, the world name the worker should import to
    # re-register the tool. Filled in by `World.__init__` if blank.
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
    static_tools: List[PluginTool] = field(default_factory=list)
    static_predicates: List[PluginPredicate] = field(default_factory=list)
    resources: Dict[str, int] = field(default_factory=dict)
    # When set, world.shared_state is initialised from this dict and
    # forwarded to sandbox workers via env. Read-only after register;
    # mutating happens on the live World instance.
    initial_shared_state: Dict[str, Any] = field(default_factory=dict)
    # When the world's python package is registered through the
    # CLI's worlds-registry, these fields record how to re-import it
    # from a fresh subprocess (the sandbox worker uses them).
    python_package: Optional[str] = None
    package_dir: Optional[Path] = None

    def build(self) -> "tuple[List[PluginTool], List[PluginPredicate]]":
        """Materialize tools and predicates for one World instance.
        Worlds that need per-instance state (their own SQLite db, etc.)
        return a fresh batch each time; worlds with static tool sets
        return the pre-registered lists every call."""
        if self.setup is not None:
            tools, preds = self.setup()
            return (
                [_coerce_to_plugin_tool(t) for t in tools],
                [_coerce_to_plugin_predicate(p) for p in preds],
            )
        return list(self.static_tools), list(self.static_predicates)


_WORLDS: Dict[str, WorldDefinition] = {}


def register_world(
    name: str,
    *,
    setup: Optional[Setup] = None,
    tools: Optional[Sequence[Union[PluginTool, Callable[..., Any]]]] = None,
    predicates: Optional[Sequence[Union[PluginPredicate, Callable[..., Any]]]] = None,
    personas_dir: Optional[Path | str] = None,
    resources: Optional[Dict[str, Any]] = None,
    shared_state: Optional[Dict[str, Any]] = None,
    python_package: Optional[str] = None,
    package_dir: Optional[Path | str] = None,
) -> WorldDefinition:
    """Register a world plugin under ``name``. Idempotent: calling
    twice for the same name overwrites the prior definition.

    Worlds with per-instance state pass ``setup``: a zero-arg callable
    returning ``(tools, predicates)`` invoked once per ``World(name)``
    construction. Worlds whose tools are stateless can pass ``tools``
    and ``predicates`` directly, as either ``PluginTool`` /
    ``PluginPredicate`` objects or functions decorated with
    ``@tool(...)`` / ``@predicate(...)``.

    ``shared_state`` declares an initial dict the runtime serialises
    into ``ENSEMBLE_SHARED_STATE`` for sandbox dispatch, so a
    sandboxed tool can read configuration the parent set without
    every world inventing its own env-var convention.

    ``python_package`` and ``package_dir`` are the import hooks the
    sandbox worker uses to re-create this exact world in a fresh
    process. They default to ``None`` and fall back to the worlds
    registry (``~/.ensemble/worlds.toml``); set them explicitly when
    the world lives outside the registry or when two installations
    of the same package coexist.
    """
    if setup is not None and (tools is not None or predicates is not None):
        raise ValueError("pass either setup= or tools=/predicates=, not both")
    pd = Path(personas_dir).expanduser().resolve() if personas_dir else None
    pkg_dir = Path(package_dir).expanduser().resolve() if package_dir else None
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
        static_tools=[_coerce_to_plugin_tool(t) for t in (tools or [])],
        static_predicates=[
            _coerce_to_plugin_predicate(p) for p in (predicates or [])
        ],
        resources=resolved_resources,
        initial_shared_state=dict(shared_state or {}),
        python_package=python_package,
        package_dir=pkg_dir,
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
# JSON-string ABI the native side expects. Both work in three modes:
#
# 1. Factory: ``tool(name, description, parameters, fn) -> PluginTool``.
# 2. Decorator factory: ``@tool(name="x", description="y", parameters={...})``
#    returning a function-decorator. The decorated function carries the
#    metadata in ``_ensemble_tool_meta`` and is otherwise unchanged, so a
#    ``World`` subclass can attach decorated methods and the runtime can
#    build bound ``PluginTool``s at instance-construction time. Used at
#    module level the function is also accepted by ``register_world``,
#    which coerces it to a ``PluginTool``.
# 3. Bare decorator: ``@tool`` on a function with no metadata. Useful when
#    the caller plans to set fields by hand on the returned object, but
#    requires ``name`` + ``description`` + ``parameters`` somewhere before
#    ``register_world`` sees it.


def _wrap_tool_fn(
    name: str,
    description: str,
    parameters: Dict[str, Any],
    fn: Callable[..., Any],
    timeout_ms: Optional[int] = None,
    resources: Optional[List[str]] = None,
    sandbox: bool = False,
    sandbox_world: Optional[str] = None,
) -> PluginTool:
    """Adapt a plain python callable into a JSON-ABI PluginTool."""

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

    return PluginTool(
        name=name,
        description=description,
        parameters=parameters,
        fn=wrapped,
        timeout_ms=timeout_ms,
        resources=resources,
        sandbox=sandbox,
        sandbox_world=sandbox_world,
    )


def tool(*args: Any, **kwargs: Any) -> Any:
    """Wrap a python function as a PluginTool, or mark it for later
    materialisation by a ``World`` subclass.

    The four-argument factory form is the original surface and still
    works for top-level use:

    ```python
    # ktbench/__init__.py
    register_world("ktbench", tools=[
        tool("submit_kernel", "Submit the kernel.", schema, submit_fn),
    ])
    ```

    The decorator-factory form is the new idiomatic shape. It works at
    module level and on a ``World`` subclass method:

    ```python
    # popcornbench/world.py
    class PopcornWorld(World):
        @tool(
            name="submit_kernel",
            description="Submit the kernel for static checks and run.",
            parameters={"type": "object", ...},
        )
        def submit_kernel(self, src: str):
            return self.runner.check_and_run(src)
    ```

    Both forms accept the optional ``timeout_ms``, ``resources``,
    ``sandbox``, and ``sandbox_world`` keyword arguments.
    """

    # Factory form: tool(name, description, parameters, fn[, **opts])
    if len(args) == 4 and callable(args[3]):
        name, description, parameters, fn = args
        return _wrap_tool_fn(name, description, parameters, fn, **kwargs)

    # Decorator-factory form: tool(name=..., description=..., parameters=...)
    # All three required keys may come either as kwargs or positionals.
    name = kwargs.pop("name", None) if "name" in kwargs else (args[0] if len(args) > 0 else None)
    description = (
        kwargs.pop("description", None)
        if "description" in kwargs
        else (args[1] if len(args) > 1 else None)
    )
    parameters = (
        kwargs.pop("parameters", None)
        if "parameters" in kwargs
        else (args[2] if len(args) > 2 else None)
    )
    timeout_ms = kwargs.pop("timeout_ms", None)
    resources = kwargs.pop("resources", None)
    sandbox = kwargs.pop("sandbox", False)
    sandbox_world = kwargs.pop("sandbox_world", None)
    if kwargs:
        raise TypeError(f"tool() got unexpected kwargs: {sorted(kwargs)}")
    if name is None or description is None or parameters is None:
        raise TypeError(
            "tool(...) needs name, description, and parameters; pass them as "
            "keyword arguments to the decorator factory, or use the four-arg "
            "factory tool(name, description, parameters, fn)."
        )

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        setattr(
            fn,
            _TOOL_META_ATTR,
            {
                "name": name,
                "description": description,
                "parameters": parameters,
                "timeout_ms": timeout_ms,
                "resources": resources,
                "sandbox": sandbox,
                "sandbox_world": sandbox_world,
            },
        )
        return fn

    return decorator


def _wrap_predicate_fn(
    name: str,
    fn: Callable[..., Any],
) -> PluginPredicate:
    """Adapt a plain python callable into a JSON-ABI PluginPredicate."""

    def wrapped(trace_json: str, args_json: str) -> bool:
        trace = json.loads(trace_json) if trace_json else []
        args = json.loads(args_json) if args_json else {}
        return bool(fn(trace, args))

    return PluginPredicate(name=name, fn=wrapped)


def predicate(*args: Any, **kwargs: Any) -> Any:
    """Wrap a python function as a PluginPredicate, or mark it for
    later materialisation by a ``World`` subclass.

    The two-argument factory form remains:

    ```python
    register_world("ktbench", predicates=[
        predicate("submit_called", submit_called_fn),
    ])
    ```

    Decorator form, at module level or on a ``World`` subclass:

    ```python
    class PopcornWorld(World):
        @predicate(name="submit_called")
        def submit_called(self, trace, args):
            return any(e["payload"].get("name") == "submit_kernel"
                       for e in trace)
    ```

    The wrapped function receives the deserialised trace (a list of
    event dicts) and the deserialised args dict; it returns a bool.
    """

    # Factory form: predicate(name, fn)
    if len(args) == 2 and callable(args[1]):
        name, fn = args
        return _wrap_predicate_fn(name, fn)

    # Decorator factory: predicate(name=...)
    name = kwargs.pop("name", None) if "name" in kwargs else (args[0] if len(args) > 0 else None)
    if kwargs:
        raise TypeError(f"predicate() got unexpected kwargs: {sorted(kwargs)}")
    if name is None:
        raise TypeError(
            "predicate(...) needs a name; pass it as predicate(name='x') or "
            "use the two-arg factory predicate(name, fn)."
        )

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        setattr(fn, _PREDICATE_META_ATTR, {"name": name})
        return fn

    return decorator


def _coerce_to_plugin_tool(obj: Any) -> PluginTool:
    """Accept a PluginTool or a function carrying tool metadata."""
    if isinstance(obj, PluginTool):
        return obj
    meta = getattr(obj, _TOOL_META_ATTR, None)
    if meta is not None and callable(obj):
        return _wrap_tool_fn(
            meta["name"],
            meta["description"],
            meta["parameters"],
            obj,
            timeout_ms=meta.get("timeout_ms"),
            resources=meta.get("resources"),
            sandbox=meta.get("sandbox", False),
            sandbox_world=meta.get("sandbox_world"),
        )
    raise TypeError(
        f"expected a PluginTool or a @tool-decorated function, got "
        f"{type(obj).__name__}"
    )


def _coerce_to_plugin_predicate(obj: Any) -> PluginPredicate:
    if isinstance(obj, PluginPredicate):
        return obj
    meta = getattr(obj, _PREDICATE_META_ATTR, None)
    if meta is not None and callable(obj):
        return _wrap_predicate_fn(meta["name"], obj)
    raise TypeError(
        f"expected a PluginPredicate or a @predicate-decorated function, got "
        f"{type(obj).__name__}"
    )
