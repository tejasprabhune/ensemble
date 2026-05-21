"""Load scenarios declared in a TOML manifest.

The manifest schema:

    [scenario.refund_storm]
    world = "agora"
    duration_turns = 30
    seed = 42

    [[scenario.refund_storm.users]]
    id = "alice"
    persona = "frustrated_power_user"
    hidden_goal = "refund_3mo"
    initial_action = { tool = "open_ticket", args = { subject = "..." } }

    [[scenario.refund_storm.agents]]
    id = "rep1"
    model = "claude-sonnet-4-5"
    tools = ["lookup", "refund", "escalate"]

    [scenario.refund_storm.graders]
    alice = "hidden_goal_resolved and not policy_violation"
    global_no_double_refunds = "not had_double_refund"

Grader expressions are evaluated by a small safe boolean parser; see
`safe_eval` below. Callable predicates with no args are supported so
worlds can expose richer hooks (e.g. `had_double_refund()`).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from .scenario import RunResult, Until, World, _REGISTRY, scenario

if sys.version_info >= (3, 11):
    import tomllib as toml_reader  # type: ignore[import-not-found]
else:
    import tomli as toml_reader  # type: ignore[import-not-found]


PredicateMap = Dict[str, Any]


def load_manifest(path: str | Path) -> Dict[str, Callable[..., Awaitable[RunResult]]]:
    """Parse a TOML manifest, build a scenario for each entry, register
    it globally, and return the new wrappers keyed by name."""
    data = toml_reader.loads(Path(path).read_text())
    section = data.get("scenario", {})
    built: Dict[str, Callable[..., Awaitable[RunResult]]] = {}
    for name, spec in section.items():
        full_name = name if "." in name else name
        built[full_name] = _build_scenario(full_name, spec)
    return built


def _build_scenario(name: str, spec: Dict[str, Any]) -> Callable[..., Awaitable[RunResult]]:
    world_name = spec.get("world")
    if not isinstance(world_name, str) or not world_name:
        raise ValueError(
            f"scenario {name!r}: 'world' field is required (set [scenario.{name}].world = ...)"
        )
    duration_turns = int(spec.get("duration_turns", 20))
    users = spec.get("users", [])
    agents = spec.get("agents", [])
    graders = spec.get("graders", {})

    @scenario(name, world=world_name)
    async def manifest_scenario(world):
        # The decorator default-runs against "noop"; the caller must
        # supply the matching world name via run_scenario(name, world).
        spawned_users = {}
        for u in users:
            user = world.spawn_user(
                id=u.get("id"),
                persona=u.get("persona"),
                hidden_goal=u.get("hidden_goal"),
                model=u.get("model", "user-model"),
            )
            spawned_users[u.get("id")] = user
            init = u.get("initial_action")
            if init:
                user.act(init["tool"], **init.get("args", {}))

        for a in agents:
            world.spawn_agent(
                id=a.get("id"),
                model=a.get("model", "claude-sonnet-4-5"),
                tools=a.get("tools", []),
            )

        yield world.until(world.turn_count > duration_turns)

        ctx = _make_grader_context(world, spawned_users)
        scores: Dict[str, float] = {}
        for key, expr in graders.items():
            scores[key] = 1.0 if safe_eval(expr, ctx) else 0.0
        yield scores

    # Stash the chosen world name on the wrapper so the CLI / tests can
    # surface it without re-reading the TOML.
    manifest_scenario.__scenario_world__ = world_name  # type: ignore[attr-defined]
    return manifest_scenario


def _make_grader_context(world: World, users: Dict[str, Any]) -> PredicateMap:
    """Build the namespace the grader expressions execute in.

    The context exposes:

    * the literals ``true`` / ``false`` and ``any_event``
    * every world predicate by name (e.g. ``had_double_refund``)
    * per-user predicates as ``<user_id>_<predicate>``, evaluated with
      ``args = {"user_id": "<user_id>"}``

    Unknown names raise during evaluation; graders that name a
    predicate the world has not registered will fail loudly rather than
    silently returning ``False``.
    """
    trace = world.trace()
    last = trace[-1] if trace else None

    ctx: Dict[str, Any] = {
        "true": True,
        "false": False,
        "any_event": last is not None,
        "turn_count": len(trace),
    }
    for name in world.predicate_names():
        ctx[name] = bool(world.evaluate_predicate(name))
    for uid in users.keys():
        for name in world.predicate_names():
            ctx[f"{uid}_{name}"] = bool(
                world.evaluate_predicate(name, {"user_id": uid})
            )
    return ctx


# A tiny recursive-descent parser for boolean expressions over a fixed
# vocabulary of names. Avoids eval / ast.literal_eval entirely. Calls
# are forbidden; only names, parens, and the operators and/or/not.


class _Tokeniser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.tokens = list(self._tokenise())
        self.i = 0

    def _tokenise(self):
        i = 0
        t = self.text
        while i < len(t):
            c = t[i]
            if c.isspace():
                i += 1
                continue
            if c in "()":
                yield c
                i += 1
                continue
            if c.isalpha() or c == "_":
                j = i
                while j < len(t) and (t[j].isalnum() or t[j] == "_"):
                    j += 1
                yield t[i:j]
                i = j
                continue
            raise ValueError(f"unexpected character {c!r} at offset {i}")

    def peek(self) -> Optional[str]:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self) -> str:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def expect(self, expected: str) -> None:
        if self.peek() != expected:
            raise ValueError(f"expected {expected!r}, got {self.peek()!r}")
        self.take()


def safe_eval(expr: str, ctx: PredicateMap) -> bool:
    """Evaluate a boolean expression over the supplied context. Only
    `and`, `or`, `not`, parens, and names appearing in `ctx` are
    permitted; calls and attribute access are rejected."""

    tok = _Tokeniser(expr)

    def parse_atom() -> bool:
        head = tok.peek()
        if head is None:
            raise ValueError("unexpected end of expression")
        if head == "(":
            tok.take()
            v = parse_or()
            tok.expect(")")
            return v
        if head == "not":
            tok.take()
            return not parse_atom()
        if head in ("and", "or"):
            raise ValueError(f"unexpected operator {head!r}")
        tok.take()
        if head not in ctx:
            raise KeyError(f"unknown name in grader expression: {head!r}")
        val = ctx[head]
        return bool(val)

    def parse_and() -> bool:
        v = parse_atom()
        while tok.peek() == "and":
            tok.take()
            v = parse_atom() and v
        return v

    def parse_or() -> bool:
        v = parse_and()
        while tok.peek() == "or":
            tok.take()
            v = parse_and() or v
        return v

    result = parse_or()
    if tok.peek() is not None:
        raise ValueError(f"trailing tokens: {tok.peek()!r}")
    return result
