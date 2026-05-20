"""Ensemble: multi-user, multi-agent RL environment framework."""

from ._native import __version__
from .scenario import (
    Agent,
    RunResult,
    Until,
    User,
    World,
    all_scenarios,
    run_scenario,
    scenario,
)
from .persona import (
    PersonaResolver,
    PersonaSpec,
    load_persona,
    register_personas_dir,
)
from .scenario_toml import load_manifest, safe_eval

__all__ = [
    "Agent",
    "PersonaResolver",
    "PersonaSpec",
    "RunResult",
    "Until",
    "User",
    "World",
    "__version__",
    "all_scenarios",
    "load_manifest",
    "load_persona",
    "register_personas_dir",
    "run_scenario",
    "safe_eval",
    "scenario",
]
