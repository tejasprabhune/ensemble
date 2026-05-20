"""Ensemble: multi-user, multi-agent RL environment framework."""

from ._native import __version__
from .scenario import (
    Agent,
    RunResult,
    Until,
    User,
    World,
    all_of,
    all_scenarios,
    any_of,
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
from .world_manifest import ManifestError, WorldManifest, load_manifest as load_world_manifest

__all__ = [
    "Agent",
    "ManifestError",
    "PersonaResolver",
    "PersonaSpec",
    "RunResult",
    "Until",
    "User",
    "World",
    "WorldManifest",
    "__version__",
    "all_of",
    "all_scenarios",
    "any_of",
    "load_manifest",
    "load_persona",
    "load_world_manifest",
    "register_personas_dir",
    "run_scenario",
    "safe_eval",
    "scenario",
]
