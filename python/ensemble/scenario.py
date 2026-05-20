"""Scenario decorator and the small Python surface around it."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ._native import World as _NativeWorld
from .env import load_dotenv
from .persona import PersonaResolver, load_persona, register_personas_dir
from .world import get_world


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


def _make_sandbox_dispatcher(world_name: str, tool_name: str) -> Callable[[str], str]:
    """Build a wrapper that dispatches a tool call to a fresh
    subprocess. The subprocess imports the world's python package
    (which re-registers tools), invokes the named tool with the
    supplied JSON args, and writes the JSON response on stdout.

    A failure to spawn or a non-zero exit is surfaced as a structured
    error effect so the calling agent gets a normal tool-result rather
    than the scheduler crashing.
    """

    import subprocess

    def dispatcher(args_json: str) -> str:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "ensemble.tool_worker",
                 "--world", world_name, "--tool", tool_name],
                input=args_json,
                capture_output=True,
                text=True,
                check=False,
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
        # The worker's last stdout line is the JSON envelope; earlier
        # lines (if any) are diagnostic. Splitting on the last
        # newline lets a worker print progress as it runs.
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
    """A halting condition. Holds a JSON-serializable spec the Rust
    side compiles into a predicate."""

    spec: Dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(self.spec)

    def __or__(self, other: "Until") -> "Until":
        return any_of(self, other)

    def __and__(self, other: "Until") -> "Until":
        return all_of(self, other)


def any_of(*conditions: Until) -> Until:
    """Halts when any of the supplied conditions fire. Flattens nested
    `any_of` for the rust side."""
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

    def say(self, target: str, text: str) -> None:
        self._native.say(target, text)

    def act(self, tool: str, **kwargs: Any) -> None:
        self._native.act_json(tool, json.dumps(kwargs))

    # The convenience predicates a scenario might want at grader time.
    # All resolve to world.evaluate_predicate(name, {"user_id": self.id}),
    # so worlds publish them by registering same-named predicates that
    # read args["user_id"]. Returning False when the world did not
    # register the predicate keeps graders robust to optional worlds.

    def predicate(self, name: str) -> bool:
        return bool(self._world.evaluate_predicate(name, {"user_id": self.id}))

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


class World:
    """A scenario-author-facing wrapper around the native World.

    Holds back-references so `User` and `Agent` can stash hidden state
    accessible after the run completes.
    """

    def __init__(
        self,
        name: str,
        backend: Optional[str] = None,
        base_url: Optional[str] = None,
        dotenv: bool | str = True,
        verbose: Optional[bool] = None,
        trace_path: Optional[str] = None,
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
        definition = get_world(name)
        # We accept "noop" implicitly so the scaffold and pure-rust
        # tests do not need to call register_world. Any other name must
        # have been registered as a plugin (typically by importing the
        # world's python package, e.g. `import plank`).
        if name != "noop" and definition is None:
            raise ValueError(
                f"no world named {name!r}; import the world's python package "
                "(which calls register_world) before constructing it, or use "
                "World(\"noop\") for a bare world"
            )
        self._native = _NativeWorld(name, backend=backend, base_url=base_url)
        if trace_path:
            self._native.set_trace_path(str(trace_path))
        self.users: List[User] = []
        self.agents: List[Agent] = []
        # Apply python-registered tools and predicates for this world.
        if definition is not None:
            tools, predicates = definition.build()
            for t in tools:
                fn = t.fn
                if getattr(t, "sandbox", False):
                    sandbox_world = t.sandbox_world or name
                    fn = _make_sandbox_dispatcher(sandbox_world, t.name)
                self._native.register_tool(
                    t.name,
                    t.description,
                    json.dumps(t.parameters),
                    fn,
                    t.timeout_ms,
                    t.resources,
                )
            for p in predicates:
                self._native.register_predicate(p.name, p.fn)
        self._announce_backend(requested=backend, verbose=verbose)

    def _announce_backend(
        self, requested: Optional[str], verbose: Optional[bool]
    ) -> None:
        chosen = self._native.backend
        if verbose is None:
            verbose = os.environ.get("ENSEMBLE_QUIET", "").strip() not in {"1", "true", "yes"}
        # Surface a key fingerprint so users can spot a stale shell env
        # var that is overriding their .env file.
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
        """Path of the current live-trace sink, if any."""
        return self._native.trace_path()

    def set_trace_path(self, path: Optional[str]) -> None:
        """Mirror every event to a JSONL file as it is appended.

        Passing ``None`` detaches the sink. Attaching mid-run picks up
        future events; previously-buffered ones are not replayed."""
        self._native.set_trace_path(str(path) if path else None)

    def spawn_user(
        self,
        id: Optional[str] = None,
        persona: Optional[str] = None,
        hidden_goal: Optional[str] = None,
        model: str = "user-model",
        system_prompt: Optional[str] = None,
        hidden_state: Optional[Dict[str, Any]] = None,
    ) -> User:
        """Spawn a user. If `persona` names a TOML registered on this
        world (see `ensemble.persona.register_personas_dir`), the
        loader pulls the system prompt template and default hidden
        state from the file. `hidden_goal` and `hidden_state` overrides
        win on top of the file defaults."""

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
            # No persona file matched; still carry the overrides as the
            # initial hidden state so graders can read them post-run.
            resolved_hidden = overrides

        native = self._native.spawn_user(
            id=id,
            persona=persona,
            hidden_goal=hidden_goal,
            model=model,
            system_prompt=resolved_prompt,
            hidden_state_json=(
                json.dumps(resolved_hidden) if resolved_hidden is not None else None
            ),
        )
        u = User(native, self, persona_spec=spec)
        self.users.append(u)
        return u

    def spawn_agent(
        self,
        id: Optional[str] = None,
        model: str = "claude-sonnet-4-5",
        tools: Optional[List[str]] = None,
        system_prompt: Optional[str] = None,
    ) -> Agent:
        native = self._native.spawn_agent(
            id=id, model=model, tools=tools, system_prompt=system_prompt
        )
        a = Agent(native, self)
        self.agents.append(a)
        return a

    def until(self, condition: Any) -> Until:
        """Coerce a condition into an `Until`. Accepts an existing
        `Until` (returns as-is) or a magic comparison result."""
        if isinstance(condition, Until):
            return condition
        if isinstance(condition, bool):
            raise TypeError(
                "world.until() received a bool; did you compare an int directly? "
                "Use world.turn_count > N (not int(world.turn_count) > N)."
            )
        raise TypeError(f"cannot coerce {type(condition).__name__} into Until")

    # Test-only scripting passthrough.
    def _mock_say(self, model: str, text: str) -> None:
        self._native._mock_say(model, text)

    def _mock_tool(self, model: str, tool: str, **args: Any) -> None:
        self._native._mock_tool(model, tool, json.dumps(args))

    def run(self, until: Until) -> List[Dict[str, Any]]:
        self._native.run_until(until.to_json())
        return [json.loads(e) for e in self._native.trace_events()]

    def simulate(self) -> "Simulation":
        """Power-user path: start the scheduler in the background and
        return an async context manager that exposes `wait_until` for
        mid-run intervention."""
        return Simulation(self)

    def trace(self) -> List[Dict[str, Any]]:
        return [json.loads(e) for e in self._native.trace_events()]

    # Predicate evaluation against the current trace. Worlds publish
    # named predicates (see ensemble-core's PredicateRegistry); both
    # the `User` convenience methods and TOML grader expressions
    # delegate here.

    def evaluate_predicate(
        self, name: str, args: Optional[Dict[str, Any]] = None
    ) -> Optional[bool]:
        args_json = json.dumps(args) if args else None
        return self._native.evaluate_predicate(name, args_json)

    def predicate_names(self) -> List[str]:
        return list(self._native.predicate_names())

    # World-level convenience predicates. Scenarios call these from
    # graders; they return False when the world has not registered the
    # named predicate, so graders stay robust to plug-in worlds.

    def had_double_refund(self) -> bool:
        return bool(self.evaluate_predicate("had_double_refund"))

    # Cost / budget API. Tool cost annotations land here through the
    # bus; the scheduler halts with BudgetExceeded when a recorded
    # cost would push the running total past a configured cap.

    def set_budget(
        self,
        unit: str,
        amount: float,
        actor: Optional[str] = None,
    ) -> None:
        """Cap spend for ``unit``. Once a recorded cost would push the
        running total past ``amount`` the scheduler halts.

        When ``actor`` is supplied the cap is scoped to that actor's
        own running total; a different actor's costs do not consume
        the cap. Per-actor and world-wide caps coexist; each is
        checked independently."""
        self._native.set_budget(unit, float(amount), actor)

    def cost_total(self, unit: str, actor: Optional[str] = None) -> float:
        """Running total for ``unit``. World-wide unless ``actor`` is
        supplied, in which case the actor's own total is returned."""
        return float(self._native.cost_total(unit, actor))

    def record_cost(
        self,
        unit: str,
        amount: float,
        actor: Optional[str] = None,
    ) -> None:
        """Manually annotate a cost (tests, external accounting). Tool
        dispatch records costs against the calling actor automatically;
        this helper is for manual or test paths."""
        self._native.record_cost(unit, float(amount), actor)


class SimulationRun:
    """The handle yielded by `async with world.simulate() as run`."""

    def __init__(self, world: "World") -> None:
        self._world = world

    async def wait_until(self, condition: Any, timeout_ms: int = 30_000) -> bool:
        """Block until `condition` fires. Yields control to the event
        loop in small slices via asyncio.to_thread so other tasks can
        proceed (e.g., test instrumentation)."""
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
    dict directly).

    ``world`` names the world this scenario expects to run against;
    the CLI uses it as the default when ``--world`` is not supplied.
    Callers may still pass a different world to the wrapper for
    cross-world testing (e.g. running a generic scenario against the
    "noop" world)."""

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
        ) -> RunResult:
            # Caller-supplied world wins; otherwise fall back to the
            # decorator's declared world; otherwise "noop" so the
            # scaffold flow still works without a world plugin.
            resolved_world = world_name or world or "noop"
            world_obj = World(
                resolved_world,
                backend=backend,
                base_url=base_url,
                trace_path=trace_path,
            )
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
                # Re-pull the trace so the grader note lands in the
                # returned trace too; the live sink already wrote it.
                trace = [json.loads(e) for e in world_obj._native.trace_events()]
                return RunResult(name=name, scores=dict(scores), trace=trace)
            else:
                # Regular async function: scenario author calls
                # world.run(until) themselves and returns the grader dict.
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
    """Synchronous helper: look up a scenario by name and run it.
    ``world_name`` overrides the world declared on the @scenario;
    leave it None to use the declared default."""
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
