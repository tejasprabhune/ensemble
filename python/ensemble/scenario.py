"""Scenario decorator and the small Python surface around it."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ._native import World as _NativeWorld
from .env import load_dotenv


@dataclass
class Until:
    """A halting condition. Holds a JSON-serializable spec the Rust
    side compiles into a predicate."""

    spec: Dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(self.spec)


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
    def __init__(self, native_user, world: "World") -> None:
        self._native = native_user
        self._world = world
        self.hidden_state: Dict[str, Any] = {}

    @property
    def id(self) -> str:
        return self._native.id

    def say(self, target: str, text: str) -> None:
        self._native.say(target, text)

    def act(self, tool: str, **kwargs: Any) -> None:
        self._native.act_json(tool, json.dumps(kwargs))

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
        self._native = _NativeWorld(name, backend=backend, base_url=base_url)
        self.users: List[User] = []
        self.agents: List[Agent] = []
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

    def spawn_user(
        self,
        id: Optional[str] = None,
        persona: Optional[str] = None,
        hidden_goal: Optional[str] = None,
        model: str = "user-model",
    ) -> User:
        native = self._native.spawn_user(
            id=id, persona=persona, hidden_goal=hidden_goal, model=model
        )
        u = User(native, self)
        self.users.append(u)
        return u

    def spawn_agent(
        self,
        id: Optional[str] = None,
        model: str = "claude-sonnet-4-5",
        tools: Optional[List[str]] = None,
    ) -> Agent:
        native = self._native.spawn_agent(id=id, model=model, tools=tools)
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


def scenario(name: str) -> Callable:
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
            world_name: str = "noop",
            backend: Optional[str] = None,
            base_url: Optional[str] = None,
        ) -> RunResult:
            world = World(world_name, backend=backend, base_url=base_url)
            if is_gen:
                gen = func(world)
                try:
                    first = await gen.__anext__()
                except StopAsyncIteration as e:
                    raise RuntimeError("scenario yielded nothing") from e
                if not isinstance(first, Until):
                    raise TypeError(
                        f"scenario must first yield an Until, got {type(first).__name__}"
                    )
                trace = world.run(first)
                try:
                    scores = await gen.__anext__()
                except StopAsyncIteration:
                    scores = {}
                if scores is None:
                    scores = {}
                return RunResult(name=name, scores=dict(scores), trace=trace)
            else:
                # Regular async function: scenario author calls
                # world.run(until) themselves and returns the grader dict.
                scores = await func(world)
                trace = [json.loads(e) for e in world._native.trace_events()]
                return RunResult(
                    name=name, scores=dict(scores or {}), trace=trace
                )

        wrapper.__scenario_name__ = name  # type: ignore[attr-defined]
        wrapper.__scenario_world__ = None  # type: ignore[attr-defined]
        _REGISTRY[name] = wrapper
        return wrapper

    return deco


def all_scenarios() -> Dict[str, Callable[..., Awaitable[RunResult]]]:
    """Return a copy of the global scenario registry."""
    return dict(_REGISTRY)


def run_scenario(
    name: str,
    world_name: str = "noop",
    backend: Optional[str] = None,
    base_url: Optional[str] = None,
) -> RunResult:
    """Synchronous helper: look up a scenario by name and run it."""
    if name not in _REGISTRY:
        raise KeyError(f"no scenario registered as {name!r}")
    return asyncio.run(
        _REGISTRY[name](world_name, backend=backend, base_url=base_url)
    )
