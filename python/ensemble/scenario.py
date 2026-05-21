"""Scenario decorator and the Python surface around it.

Two ways to build a world. The factory form,
``register_world(name, setup=...)``, populates a global registry the
``World(name)`` constructor reads. The subclass form,
``class MyWorld(World)`` with methods decorated by ``@tool(...)`` /
``@predicate(...)``, auto-registers itself when first instantiated.

The Python surface in this module covers both. ``World.__init__``
inspects the runtime class: when called on a subclass it walks the
subclass for decorated methods, calls ``self.setup()`` if defined,
and forwards the resulting tools and predicates to the native side.
``world.shared_state`` is a mutable JSON-serialisable dict the
runtime forwards to sandbox workers via the
``ENSEMBLE_SHARED_STATE`` environment variable.

``world.log_event(kind, payload)`` and ``world.log_note(text)`` are
the public trace-emit helpers. ``spawn_agent`` automatically emits
an ``agent_spawned`` event with the resolved model and system
prompt, so worlds and viewers no longer need a per-scenario helper.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, ClassVar, Dict, List, Optional

from ._native import World as _NativeWorld
from .env import load_dotenv
from .persona import PersonaResolver, load_persona, register_personas_dir
from .world import (
    _PREDICATE_META_ATTR,
    _TOOL_META_ATTR,
    PluginPredicate,
    PluginTool,
    WorldDefinition,
    _coerce_to_plugin_predicate,
    _coerce_to_plugin_tool,
    _wrap_predicate_fn,
    _wrap_tool_fn,
    get_world,
)


def _log_grader_scores(world_obj: "World", scenario_name: str, scores: Dict[str, Any]) -> None:
    """Append a structured note to the trace summarising the grader
    output. Lets the trace stand on its own (the viewer and any
    downstream consumer can read the final scores without consulting
    a separate RunResult object) and gives the live trace writer a
    final event to flush."""
    try:
        payload = {
            "kind": "grader",
            "scenario": scenario_name,
            "scores": {str(k): float(v) for k, v in dict(scores).items()},
        }
    except (TypeError, ValueError):
        payload = {"kind": "grader", "scenario": scenario_name, "scores": dict(scores)}
    try:
        world_obj._native.log_note("grader: " + json.dumps(payload))
    except AttributeError:
        pass


SANDBOX_SHARED_STATE_ENV = "ENSEMBLE_SHARED_STATE"
SANDBOX_PACKAGE_ENV = "ENSEMBLE_SANDBOX_PACKAGE"
SANDBOX_PACKAGE_DIR_ENV = "ENSEMBLE_SANDBOX_PACKAGE_DIR"


def _make_sandbox_dispatcher(
    world_name: str,
    tool_name: str,
    world_ref: "World",
) -> Callable[[str], str]:
    """Build a wrapper that dispatches a tool call to a fresh
    subprocess. The worker imports the world's python package and
    re-registers the tool from scratch; the parent forwards
    ``shared_state`` (JSON-serialised) plus the package + package_dir
    hint so the worker resolves the same world even when
    ``~/.ensemble/worlds.toml`` would have given it a different one
    or nothing at all.
    """

    import subprocess

    def dispatcher(args_json: str) -> str:
        env = os.environ.copy()
        # Forward shared_state at call time so each dispatch sees the
        # parent's latest snapshot; closures-over-state do not cross
        # the boundary, but this dict does.
        try:
            env[SANDBOX_SHARED_STATE_ENV] = json.dumps(world_ref.shared_state)
        except (TypeError, ValueError):
            env[SANDBOX_SHARED_STATE_ENV] = "{}"
        # Tell the worker exactly which python package to import. The
        # worker still falls back to the worlds registry if these are
        # unset, but the explicit path eliminates the "registry says
        # one thing, parent loaded another" failure mode the round-2
        # KTBench feedback called out.
        if world_ref._sandbox_python_package:
            env[SANDBOX_PACKAGE_ENV] = world_ref._sandbox_python_package
        if world_ref._sandbox_package_dir:
            env[SANDBOX_PACKAGE_DIR_ENV] = str(world_ref._sandbox_package_dir)
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "ensemble.tool_worker",
                 "--world", world_name, "--tool", tool_name],
                input=args_json,
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
        except FileNotFoundError as e:
            return json.dumps({
                "effect": {
                    "ok": False,
                    "tool": tool_name,
                    "summary": f"sandbox worker not found: {e}",
                }
            })
        if proc.returncode != 0:
            stderr_tail = (proc.stderr or "").strip()[-1000:]
            return json.dumps({
                "effect": {
                    "ok": False,
                    "tool": tool_name,
                    "summary": (
                        f"sandbox worker exited {proc.returncode}.\n"
                        f"{stderr_tail}"
                    ),
                }
            })
        stdout = (proc.stdout or "").strip()
        if not stdout:
            return json.dumps({
                "effect": {
                    "ok": False,
                    "tool": tool_name,
                    "summary": "sandbox worker produced no output",
                }
            })
        last_line = stdout.splitlines()[-1]
        return last_line

    return dispatcher


@dataclass
class Until:
    """A halting condition. Holds a JSON-serialisable spec the rust
    side compiles into a predicate."""

    spec: Dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(self.spec)

    def __or__(self, other: "Until") -> "Until":
        return any_of(self, other)

    def __and__(self, other: "Until") -> "Until":
        return all_of(self, other)


def any_of(*conditions: Until) -> Until:
    """Halts when any of the supplied conditions fire. Flattens
    nested `any_of` so the wire spec stays shallow."""
    parts: List[Dict[str, Any]] = []
    for c in conditions:
        if c.spec.get("kind") == "any_of":
            parts.extend(c.spec.get("parts", []))
        else:
            parts.append(c.spec)
    return Until({"kind": "any_of", "parts": parts})


def all_of(*conditions: Until) -> Until:
    """Halts when all of the supplied conditions hold simultaneously."""
    parts: List[Dict[str, Any]] = []
    for c in conditions:
        if c.spec.get("kind") == "all_of":
            parts.extend(c.spec.get("parts", []))
        else:
            parts.append(c.spec)
    return Until({"kind": "all_of", "parts": parts})


def until_predicate(name: str, **args: Any) -> Until:
    """Halt when a named world predicate returns true. The rust
    scheduler evaluates the predicate against the live trace each
    tick; composes with turn-count via ``|`` / ``&`` so the common
    "stop on submit, cap at N turns" shape is one expression."""
    spec: Dict[str, Any] = {"kind": "predicate", "name": name}
    if args:
        spec["args"] = args
    return Until(spec)


class TurnCount:
    """A sentinel that supports rich comparison ops so users can write
    `world.turn_count > 30` and get back an `Until`. Coerces to int
    via `world.current_turn_count()` for post-run inspection."""

    def __init__(self, world: "World") -> None:
        self._world = world

    def _value(self) -> int:
        return int(self._world._native.current_turn_count())

    def __gt__(self, n: int) -> Until:
        return Until({"kind": "turn_count_gt", "n": int(n)})

    def __ge__(self, n: int) -> Until:
        return Until({"kind": "turn_count_ge", "n": int(n)})

    def __int__(self) -> int:
        return self._value()

    def __repr__(self) -> str:
        return f"TurnCount({self._value()})"


@dataclass
class RunResult:
    name: str
    scores: Dict[str, float] = field(default_factory=dict)
    trace: List[Dict[str, Any]] = field(default_factory=list)


class PredicateError(KeyError):
    """Raised when ``world.evaluate_predicate`` is called with a
    name no predicate was registered under."""


_PREDICATE_NO_DEFAULT = object()


class User:
    def __init__(
        self,
        native_user,
        world: "World",
        persona_spec: Optional["PersonaSpec"] = None,
    ) -> None:
        self._native = native_user
        self._world = world
        self._persona = persona_spec

    @property
    def id(self) -> str:
        return self._native.id

    @property
    def hidden_state(self) -> Dict[str, Any]:
        """Snapshot of the user's hidden state. For a persona-backed
        user, this reflects whatever the persona has mutated to;
        otherwise it is empty."""
        raw = self._native.hidden_state_json()
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, dict) else {}

    @property
    def persona(self) -> Optional["PersonaSpec"]:
        return self._persona

    @property
    def backend_info(self) -> Optional[Dict[str, Any]]:
        info = self._native.backend_info()
        return info if isinstance(info, dict) else None

    def say(self, target: str, text: str) -> None:
        self._native.say(target, text)

    def act(self, tool: str, **kwargs: Any) -> None:
        self._native.act_json(tool, json.dumps(kwargs))

    def predicate(self, name: str, default: Any = False) -> bool:
        """Per-user predicate convenience. Defaults to False on
        unknown predicate names so portable graders that target
        multiple worlds with different predicate sets stay robust;
        pass ``default=...`` to override, or call
        ``world.evaluate_predicate`` directly to get the raise-loudly
        behaviour."""
        try:
            return bool(self._world.evaluate_predicate(
                name, {"user_id": self.id}, default=default,
            ))
        except PredicateError:
            return bool(default)

    def hidden_goal_resolved(self) -> bool:
        return self.predicate("hidden_goal_resolved")

    def was_redirected_to_upgrade(self) -> bool:
        return self.predicate("was_redirected_to_upgrade")

    def __repr__(self) -> str:
        return f"<User id={self.id!r}>"


class Agent:
    def __init__(self, native_agent, world: "World") -> None:
        self._native = native_agent
        self._world = world

    @property
    def id(self) -> str:
        return self._native.id

    def say(self, target: str, text: str) -> None:
        self._native.say(target, text)

    def __repr__(self) -> str:
        return f"<Agent id={self.id!r}>"


class _ExternalAgent:
    """Stand-in for an agent slot whose turns are driven by a
    connected external client (an MCP-aware tool). Implements the
    same minimal surface as ``Agent`` (``id``, ``say``) so a scenario
    function bound against it does not need to special-case the slot.
    Outbound ``say`` goes through ``world.external_send_as`` so the
    trace records the message as having come from the slot."""

    def __init__(self, agent_id: str, world: "World") -> None:
        self.id = agent_id
        self._world = world

    def say(self, target: str, text: str) -> None:
        self._world._native.external_send_as(self.id, target, text)

    def __repr__(self) -> str:
        return f"<ExternalAgent id={self.id!r}>"


class World:
    """Scenario-author-facing wrapper around the native World.

    Used in two shapes:

    1. ``World("plank")`` references a world registered via
       ``register_world("plank", ...)``.
    2. ``class PopcornWorld(World)`` with ``@tool`` / ``@predicate``
       decorated methods and an optional ``setup(self)``. Subclasses
       auto-derive a world name from ``world_name`` (a class
       attribute) or the lower-cased class name.
    """

    # Subclasses override to override the world name; default falls
    # back to the lower-cased class name.
    world_name: ClassVar[Optional[str]] = None

    def __init__(
        self,
        name: Optional[str] = None,
        backend: Optional[str] = None,
        base_url: Optional[str] = None,
        dotenv: bool | str = True,
        verbose: Optional[bool] = None,
        trace_path: Optional[str] = None,
        external_agent_id: Optional[str] = None,
    ) -> None:
        if dotenv:
            path = ".env"
            override = False
            if isinstance(dotenv, str):
                if dotenv == "override":
                    override = True
                else:
                    path = dotenv
            load_dotenv(path, override=override)

        is_subclass = type(self) is not World
        if is_subclass:
            resolved_name = (
                name
                or self.world_name
                or type(self).__name__.lower()
            )
            definition = get_world(resolved_name)
        else:
            resolved_name = name or "noop"
            definition = get_world(resolved_name)
            if resolved_name != "noop" and definition is None:
                raise ValueError(
                    f"no world named {resolved_name!r}; import the world's "
                    "python package (which calls register_world) before "
                    "constructing it, or use World(\"noop\") for a bare "
                    "world. Subclasses of World skip the registry; if you "
                    "meant to write a subclass, define `class MyWorld(World)` "
                    "and instantiate that instead."
                )

        self._native = _NativeWorld(resolved_name, backend=backend, base_url=base_url)
        if trace_path:
            self._native.set_trace_path(str(trace_path))
        self.users: List[User] = []
        self.agents: List[Agent] = []
        self._external_agent_id: Optional[str] = external_agent_id
        self._external_agent: Optional[Agent] = None
        self._external_agent_tools: List[str] = []

        # Per-instance state the scenario author can mutate. Survives
        # tool calls in-process. For sandboxed tools the runtime
        # serialises it into the worker's environment via
        # ENSEMBLE_SHARED_STATE; mutations the worker makes do *not*
        # propagate back, since the worker process exits after each
        # call. Treat it as a configuration channel for sandboxed
        # tools and a per-instance state bag for in-process ones.
        self.shared_state: Dict[str, Any] = {}
        # Sandbox-worker hints. ``shared_state`` plus these two strings
        # are everything the worker needs to re-create the same world
        # in a fresh interpreter.
        self._sandbox_python_package: Optional[str] = None
        self._sandbox_package_dir: Optional[Path] = None

        # Predicates registered by post-construction code (subclass
        # walker, scenario callsites). Tracked here so
        # ``predicate_names`` and ``evaluate_predicate(default=...)``
        # can answer authoritatively without round-tripping to the
        # rust side every call.
        self._registered_predicate_names: set[str] = set()

        # Per-world default models, populated from the world plugin's
        # register_world(default_user_model=..., default_agent_model=...)
        # call. spawn_user/spawn_agent fall back to these before the
        # framework-wide sentinels.
        self._default_user_model: Optional[str] = None
        self._default_agent_model: Optional[str] = None

        # Counters that back auto-generated actor ids when the caller
        # does not pass one.
        self._next_user_index: int = 1
        self._next_agent_index: int = 1
        self._next_opener_index: int = 1

        if definition is not None:
            self._apply_definition(definition)

        if is_subclass:
            # Subclass path: gather decorated methods and call
            # setup() so the subclass has somewhere to build its
            # state. Important ordering: setup runs *before* the
            # decorated-method walker so a setup that initialises
            # ``self.db`` is in place when the tool wrappers run.
            self._sandbox_python_package = self._sandbox_python_package or type(self).__module__.split(".")[0]
            if hasattr(self, "setup"):
                self.setup()
            self._register_subclass_members()

        self._announce_backend(requested=backend, verbose=verbose)

    def _apply_definition(self, definition: WorldDefinition) -> None:
        for rname, permits in definition.resources.items():
            self._native.declare_resource(rname, permits)
        if definition.initial_shared_state:
            self.shared_state = dict(definition.initial_shared_state)
        if definition.python_package and not self._sandbox_python_package:
            self._sandbox_python_package = definition.python_package
        if definition.package_dir and not self._sandbox_package_dir:
            self._sandbox_package_dir = definition.package_dir
        if definition.default_user_model:
            self._default_user_model = definition.default_user_model
        if definition.default_agent_model:
            self._default_agent_model = definition.default_agent_model
        tools, predicates = definition.build()
        for t in tools:
            self._register_native_tool(t)
        for p in predicates:
            self._register_native_predicate(p)

    def _register_subclass_members(self) -> None:
        """Walk this instance's class for ``@tool`` and ``@predicate``
        decorated methods, build per-instance ``PluginTool`` /
        ``PluginPredicate`` objects bound to ``self``, and forward
        them to the native registry. Decorated methods on a base
        ``World`` (none today) would be picked up too."""
        cls = type(self)
        # MRO walk so a subclass that mixes in another World subclass
        # inherits its decorated methods (or shadows them by
        # redefining the method on the more-derived class).
        seen: set[str] = set()
        for klass in cls.__mro__:
            if klass is World or klass is object:
                continue
            for attr_name, attr in klass.__dict__.items():
                if attr_name in seen or not callable(attr):
                    continue
                meta_tool = getattr(attr, _TOOL_META_ATTR, None)
                meta_pred = getattr(attr, _PREDICATE_META_ATTR, None)
                if meta_tool is None and meta_pred is None:
                    continue
                bound = getattr(self, attr_name)
                if meta_tool is not None:
                    plugin = _wrap_tool_fn(
                        meta_tool["name"],
                        meta_tool["description"],
                        meta_tool["parameters"],
                        bound,
                        timeout_ms=meta_tool.get("timeout_ms"),
                        resources=meta_tool.get("resources"),
                        sandbox=meta_tool.get("sandbox", False),
                        sandbox_world=meta_tool.get("sandbox_world"),
                    )
                    self._register_native_tool(plugin)
                if meta_pred is not None:
                    plugin = _wrap_predicate_fn(meta_pred["name"], bound)
                    self._register_native_predicate(plugin)
                seen.add(attr_name)

    def _register_native_tool(self, t: PluginTool) -> None:
        fn = t.fn
        if getattr(t, "sandbox", False):
            sandbox_world = t.sandbox_world or self.name
            fn = _make_sandbox_dispatcher(sandbox_world, t.name, self)
        self._native.register_tool(
            t.name,
            t.description,
            json.dumps(t.parameters),
            fn,
            t.timeout_ms,
            t.resources,
        )

    def _register_native_predicate(self, p: PluginPredicate) -> None:
        self._native.register_predicate(p.name, p.fn)
        self._registered_predicate_names.add(p.name)

    def _announce_backend(
        self, requested: Optional[str], verbose: Optional[bool]
    ) -> None:
        chosen = self._native.backend
        if verbose is None:
            verbose = os.environ.get("ENSEMBLE_QUIET", "").strip() not in {"1", "true", "yes"}
        key_hint = ""
        env_var = {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
        }.get(chosen)
        if env_var:
            raw = os.environ.get(env_var, "")
            if raw:
                key_hint = f" key={raw[:6]}..."

        note: str
        if requested is None and chosen == "mock":
            note = (
                "ensemble: backend=mock (default). "
                "Pass backend='auto' to pick anthropic/openai from the env, "
                "or backend='anthropic' / 'openai' / 'vllm' explicitly."
            )
        elif requested == "auto" and chosen == "mock":
            note = (
                "ensemble: backend=mock (no ANTHROPIC_API_KEY or OPENAI_API_KEY "
                "found in env or .env; falling back to deterministic mock)"
            )
        elif requested == "auto":
            note = f"ensemble: backend={chosen} (auto-detected from environment){key_hint}"
        else:
            note = f"ensemble: backend={chosen}{key_hint}"
        # Print BEFORE the first LLM call so the user can see what
        # backend is about to do work, not after it has already
        # silently fallen back to mock.
        if verbose:
            print(note, file=sys.stderr)
        try:
            self._native.log_note(note)
        except AttributeError:
            pass

    @property
    def name(self) -> str:
        return self._native.name

    @property
    def backend(self) -> str:
        return self._native.backend

    @property
    def turn_count(self) -> TurnCount:
        return TurnCount(self)

    @property
    def trace_path(self) -> Optional[str]:
        return self._native.trace_path()

    def set_trace_path(self, path: Optional[str]) -> None:
        self._native.set_trace_path(str(path) if path else None)

    def spawn_user(
        self,
        id: Optional[str] = None,
        persona: Optional[str] = None,
        hidden_goal: Optional[str] = None,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        hidden_state: Optional[Dict[str, Any]] = None,
        interactive: bool = True,
    ) -> User:
        """Spawn a user. ``persona`` looks up a TOML registered on this
        world; ``hidden_state`` and ``hidden_goal`` override file
        defaults. Trained personas (with an ``adapter_name``) route
        through a per-user vLLM backend.

        When ``model`` is omitted the world's ``default_user_model``
        (set on ``register_world`` or in ``world.toml``) is used; if
        the world declares none either, the framework-wide sentinel
        ``"user-model"`` is used so the trace records the actor's
        intended role.

        When ``id`` is omitted an auto-generated id (``user-1``,
        ``user-2``, ...) is assigned so the smoke scenario can elide
        the field.

        ``interactive=False`` makes the user silent on inbound messages:
        the scheduler still records what the agent said into the user's
        history, but the user does not call the backend to produce a
        reply. The scenario can still drive the conversation through
        ``user.say(...)``. Use this for scripted personas whose only
        job is to deliver one or more seed messages and then stay
        silent, so the run does not waste backend calls (and 404 against
        sentinel model names like ``"user-model"``)."""

        if id is None:
            id = f"user-{self._next_user_index}"
            self._next_user_index += 1
        if model is None:
            model = self._default_user_model or "user-model"

        overrides: Dict[str, Any] = {}
        if hidden_goal is not None:
            overrides["hidden_goal"] = hidden_goal
        if hidden_state:
            overrides.update(hidden_state)

        spec = None
        resolved_prompt = system_prompt
        resolved_hidden: Optional[Dict[str, Any]] = None
        if persona:
            resolver = PersonaResolver(self.name)
            spec = resolver.resolve(persona, hidden_overrides=overrides)
            if spec is not None:
                if resolved_prompt is None:
                    resolved_prompt = spec.system_prompt
                resolved_hidden = spec.hidden_state
        if spec is None and overrides:
            resolved_hidden = overrides

        vllm_base_url: Optional[str] = None
        vllm_adapter: Optional[str] = None
        if spec is not None and spec.is_trained:
            vllm_base_url = spec.serve_url or os.environ.get("ENSEMBLE_VLLM_BASE_URL")
            if vllm_base_url:
                vllm_adapter = spec.adapter_name
            else:
                note = (
                    f"persona {persona!r} declares mode=\"trained\" with "
                    f"adapter_name={spec.adapter_name!r} but no "
                    "persona.training.serve_url and no "
                    "ENSEMBLE_VLLM_BASE_URL; the spawned user is using "
                    "the world's default backend. Set one of those to "
                    "route through the trained adapter."
                )
                try:
                    self._native.log_note("trained-persona fallback: " + note)
                except AttributeError:
                    pass

        native = self._native.spawn_user(
            id=id,
            persona=persona,
            hidden_goal=hidden_goal,
            model=model,
            system_prompt=resolved_prompt,
            hidden_state_json=(
                json.dumps(resolved_hidden) if resolved_hidden is not None else None
            ),
            vllm_base_url=vllm_base_url,
            vllm_adapter=vllm_adapter,
            interactive=interactive,
        )
        u = User(native, self, persona_spec=spec)
        self.users.append(u)
        # Emit a structured spawn event so trace consumers can render
        # the user's resolved persona, model, and system prompt
        # without each world having to log_note them by hand.
        self.log_event("user_spawned", {
            "actor_id": u.id,
            "model": model,
            "persona": persona,
            "system_prompt": resolved_prompt,
            "hidden_state": resolved_hidden,
        })
        return u

    def spawn_agent(
        self,
        id: Optional[str] = None,
        model: Optional[str] = None,
        tools: Optional[List[str]] = None,
        system_prompt: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Agent:
        """Spawn an agent. ``params`` is an open dict the runtime
        forwards into the per-actor ``CompletionRequest`` so a single
        agent can override the backend's defaults (temperature,
        max_tokens, reasoning_effort, top_p, ...). Unknown keys are
        forwarded verbatim; the backend chooses what to do with
        them.

        When ``model`` is omitted the world's ``default_agent_model``
        is used; the framework-wide fallback is ``claude-sonnet-4-5``.
        When ``id`` is omitted an auto-generated id (``agent-1``,
        ``agent-2``, ...) is assigned."""
        if id is None:
            id = f"agent-{self._next_agent_index}"
            self._next_agent_index += 1
        if model is None:
            model = self._default_agent_model or "claude-sonnet-4-5"
        if id is not None and id == self._external_agent_id:
            agent = self._spawn_external_agent(id, list(tools or []))
            self.log_event("agent_spawned", {
                "actor_id": agent.id,
                "model": "external",
                "tools": list(tools or []),
                "system_prompt": system_prompt,
                "external": True,
            })
            return agent
        # Forward params via JSON when the native side accepts them.
        # We try both signatures so older _native builds still work.
        try:
            native = self._native.spawn_agent(
                id=id,
                model=model,
                tools=tools,
                system_prompt=system_prompt,
                params_json=json.dumps(params) if params else None,
            )
        except TypeError:
            native = self._native.spawn_agent(
                id=id, model=model, tools=tools, system_prompt=system_prompt,
            )
        a = Agent(native, self)
        self.agents.append(a)
        self.log_event("agent_spawned", {
            "actor_id": a.id,
            "model": model,
            "tools": list(tools) if tools is not None else None,
            "system_prompt": system_prompt,
            "params": params,
        })
        return a

    def _spawn_external_agent(self, id: str, tools: List[str]) -> "_ExternalAgent":
        self._native.register_external_agent(id, tools)
        agent = _ExternalAgent(id, self)
        self._external_agent = agent
        self._external_agent_tools = list(tools)
        self.agents.append(agent)
        return agent

    def until(self, condition: Any) -> Until:
        """Coerce a condition into an `Until`. Accepts an existing
        `Until` (returned as-is) or a magic comparison result."""
        if isinstance(condition, Until):
            return condition
        if isinstance(condition, bool):
            raise TypeError(
                "world.until() received a bool; did you compare an int directly? "
                "Use world.turn_count > N (not int(world.turn_count) > N), or "
                "compose with until_predicate(name) for halt-on-predicate."
            )
        raise TypeError(f"cannot coerce {type(condition).__name__} into Until")

    def until_predicate(self, name: str, **args: Any) -> Until:
        """Build an Until that fires when the named predicate returns
        true. ``until_predicate('submit_called') | (world.turn_count > 30)``
        is the canonical "stop on submit, give up after N turns"
        shape."""
        if name not in self._registered_predicate_names and name not in self.predicate_names():
            raise PredicateError(
                f"no predicate named {name!r} on world {self.name!r}; "
                f"registered: {sorted(self.predicate_names())}"
            )
        return until_predicate(name, **args)

    def _mock_say(self, model: str, text: str) -> None:
        self._native._mock_say(model, text)

    def _mock_tool(self, model: str, tool: str, **args: Any) -> None:
        self._native._mock_tool(model, tool, json.dumps(args))

    def apply(self, tool: str, **kwargs: Any) -> Dict[str, Any]:
        """Run a tool as a system-level mutation with no actor
        attribution. Records the ToolCall, ToolResult, and any
        StateDiff in the trace; returns the parsed envelope."""
        raw = self._native.apply(tool, json.dumps(kwargs))
        return json.loads(raw)

    def run(self, until: Until) -> List[Dict[str, Any]]:
        self._native.run_until(until.to_json())
        return [json.loads(e) for e in self._native.trace_events()]

    def simulate(self) -> "Simulation":
        return Simulation(self)

    def trace(self) -> List[Dict[str, Any]]:
        return [json.loads(e) for e in self._native.trace_events()]

    def evaluate_predicate(
        self,
        name: str,
        args: Optional[Dict[str, Any]] = None,
        *,
        default: Any = _PREDICATE_NO_DEFAULT,
    ) -> Any:
        """Evaluate a registered predicate against the current trace.

        Unknown predicate names raise :class:`PredicateError` by
        default. Pass ``default=...`` to opt into the old silent
        behaviour (portable graders that target multiple worlds with
        different predicate sets sometimes want this); typo-shaped
        bugs in your own world's predicate names should not silently
        score zero, so the raise-by-default reads loudly in CI.
        """
        args_json = json.dumps(args) if args else None
        result = self._native.evaluate_predicate(name, args_json)
        if result is None:
            if default is _PREDICATE_NO_DEFAULT:
                raise PredicateError(
                    f"predicate {name!r} is not registered on world "
                    f"{self.name!r}; registered: {sorted(self.predicate_names())}"
                )
            return default
        return result

    def predicate_names(self) -> List[str]:
        """Names of every predicate the world has registered.
        Combines what the rust side reports with predicates the
        python wrapper added so freshly registered subclass methods
        show up immediately."""
        names = set(self._native.predicate_names())
        names.update(self._registered_predicate_names)
        return sorted(names)

    def tool_names(self) -> List[str]:
        """Names of every tool the world has registered."""
        return list(self._native.tool_names())

    def actor_hidden_state(self, actor_id: str) -> Dict[str, Any]:
        """Snapshot of the hidden state for ``actor_id``. Returns an
        empty dict for actors with no hidden state attached (most
        agents). Useful for graders that read a reviewer agent's
        verdict after the run completes."""
        for u in self.users:
            if u.id == actor_id:
                return u.hidden_state
        return {}

    def log_note(self, text: str) -> None:
        """Append a free-form system note to the trace. Use for
        ad-hoc human-readable annotations; for structured events
        prefer :meth:`log_event`."""
        try:
            self._native.log_note(text)
        except AttributeError:
            pass

    def log_event(self, kind: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Append a structured event to the trace. The runtime
        encodes it as a system note whose body is
        ``{"kind": kind, **payload}``; the viewer renders known
        kinds (``agent_spawned``, ``user_spawned``, ``grader``,
        ``problem_prompt``) specially and falls back to a generic
        notes panel for unknown kinds."""
        body: Dict[str, Any] = {"kind": kind}
        if payload:
            for k, v in payload.items():
                body[k] = v
        try:
            self._native.log_note(json.dumps(body))
        except (AttributeError, TypeError):
            pass

    def had_double_refund(self) -> bool:
        return bool(self.evaluate_predicate("had_double_refund", default=False))

    def set_budget(
        self,
        unit: str,
        amount: float,
        actor: Optional[str] = None,
    ) -> None:
        self._native.set_budget(unit, float(amount), actor)

    def cost_total(self, unit: str, actor: Optional[str] = None) -> float:
        return float(self._native.cost_total(unit, actor))

    def record_cost(
        self,
        unit: str,
        amount: float,
        actor: Optional[str] = None,
    ) -> None:
        self._native.record_cost(unit, float(amount), actor)


class SimulationRun:
    def __init__(self, world: "World") -> None:
        self._world = world

    async def wait_until(self, condition: Any, timeout_ms: int = 30_000) -> bool:
        until = self._world.until(condition)
        return await asyncio.to_thread(
            self._world._native.wait_for_until, until.to_json(), timeout_ms
        )


class Simulation:
    def __init__(self, world: "World") -> None:
        self._world = world
        self._run: Optional[SimulationRun] = None

    async def __aenter__(self) -> SimulationRun:
        self._world._native.start_scheduler()
        self._run = SimulationRun(self._world)
        return self._run

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._world._native.stop_scheduler()


_REGISTRY: Dict[str, Callable[[], Awaitable[RunResult]]] = {}


def scenario(name: str, *, world: Optional[str] = None) -> Callable:
    """Register a scenario. The wrapped function may be either an
    async generator (yield once with the until, yield once with the
    grader dict) or a regular async function (returns the grader
    dict directly)."""

    def deco(func: Callable) -> Callable:
        is_gen = inspect.isasyncgenfunction(func)
        is_coro = inspect.iscoroutinefunction(func)
        if not (is_gen or is_coro):
            raise TypeError(
                "scenario must be `async def` (with yield) or `async def` (regular)"
            )

        async def wrapper(
            world_name: Optional[str] = None,
            backend: Optional[str] = None,
            base_url: Optional[str] = None,
            trace_path: Optional[str] = None,
            external_agent_id: Optional[str] = None,
            on_world_constructed: Optional[Callable[["World"], None]] = None,
        ) -> RunResult:
            resolved_world = world_name or world or "noop"
            world_obj = World(
                resolved_world,
                backend=backend,
                base_url=base_url,
                trace_path=trace_path,
                external_agent_id=external_agent_id,
            )
            if on_world_constructed is not None:
                on_world_constructed(world_obj)
            if is_gen:
                gen = func(world_obj)
                try:
                    first = await gen.__anext__()
                except StopAsyncIteration as e:
                    raise RuntimeError("scenario yielded nothing") from e
                if not isinstance(first, Until):
                    raise TypeError(
                        f"scenario must first yield an Until, got {type(first).__name__}"
                    )
                trace = world_obj.run(first)
                try:
                    scores = await gen.__anext__()
                except StopAsyncIteration:
                    scores = {}
                if scores is None:
                    scores = {}
                _log_grader_scores(world_obj, name, scores)
                trace = [json.loads(e) for e in world_obj._native.trace_events()]
                return RunResult(name=name, scores=dict(scores), trace=trace)
            else:
                scores = await func(world_obj)
                _log_grader_scores(world_obj, name, scores or {})
                trace = [json.loads(e) for e in world_obj._native.trace_events()]
                return RunResult(
                    name=name, scores=dict(scores or {}), trace=trace
                )

        wrapper.__scenario_name__ = name  # type: ignore[attr-defined]
        wrapper.__scenario_world__ = world  # type: ignore[attr-defined]
        _REGISTRY[name] = wrapper
        return wrapper

    return deco


def all_scenarios() -> Dict[str, Callable[..., Awaitable[RunResult]]]:
    """Return a copy of the global scenario registry."""
    return dict(_REGISTRY)


def run_scenario(
    name: str,
    world_name: Optional[str] = None,
    backend: Optional[str] = None,
    base_url: Optional[str] = None,
    trace_path: Optional[str] = None,
) -> RunResult:
    if name not in _REGISTRY:
        raise KeyError(f"no scenario registered as {name!r}")
    return asyncio.run(
        _REGISTRY[name](
            world_name,
            backend=backend,
            base_url=base_url,
            trace_path=trace_path,
        )
    )
