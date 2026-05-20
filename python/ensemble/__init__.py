"""Ensemble: multi-user, multi-agent RL environment framework."""

from ._native import __version__
from .scenario import (
    Agent,
    PredicateError,
    RunResult,
    Until,
    User,
    World,
    all_of,
    all_scenarios,
    any_of,
    run_scenario,
    scenario,
    until_predicate,
)
from .persona import (
    PersonaResolver,
    PersonaSpec,
    load_persona,
    register_personas_dir,
)
from .scenario_toml import load_manifest, safe_eval
from .world import (
    PluginPredicate,
    PluginTool,
    WorldDefinition,
    get_world,
    predicate,
    register_world,
    registered_world_names,
    tool,
)
from .world_manifest import ManifestError, WorldManifest, load_manifest as load_world_manifest

__all__ = [
    "Agent",
    "ManifestError",
    "PersonaResolver",
    "PersonaSpec",
    "PluginPredicate",
    "PluginTool",
    "PredicateError",
    "RunResult",
    "Until",
    "User",
    "World",
    "WorldDefinition",
    "WorldManifest",
    "__version__",
    "all_of",
    "all_scenarios",
    "any_of",
    "get_world",
    "load_manifest",
    "load_persona",
    "load_world_manifest",
    "predicate",
    "register_personas_dir",
    "register_world",
    "registered_world_names",
    "run_scenario",
    "safe_eval",
    "scenario",
    "tool",
    "until_predicate",
]
