"""Parse a world's ``world.toml`` manifest.

Worlds are plugins: ensemble discovers them through a manifest at the
root of the world's directory. The manifest declares the world's name,
where the rust crate and python package live, and small declarative
extras (default personas, default tools, resources). Loading a manifest
does not import the python package; the worlds registry does that
separately when a scenario asks for ``World("plank")``.

The schema is intentionally small. Fields that downstream phases need
(``resources`` for phase 5, ``cli`` for world-specific subcommands)
are tolerated but unused by this loader.

A minimal manifest::

    [world]
    name = "plank"
    python_package = "plank"
    rust_crate = "world"
    personas_dir = "personas"

    [[world.default_tools]]
    name = "open_ticket"

    [[world.default_personas]]
    name = "frustrated_power_user"
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

if sys.version_info >= (3, 11):
    import tomllib as _toml
else:
    import tomli as _toml  # type: ignore[import-not-found]


MANIFEST_FILENAME = "world.toml"


class ManifestError(Exception):
    """Raised when a world.toml is missing or malformed."""


@dataclass
class WorldManifest:
    name: str
    root: Path
    python_package: str
    rust_crate: Optional[str] = None
    personas_dir: Optional[Path] = None
    default_tools: List[str] = field(default_factory=list)
    default_personas: List[str] = field(default_factory=list)
    default_user_model: Optional[str] = None
    default_agent_model: Optional[str] = None
    resources: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    cli: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    def python_package_dir(self) -> Path:
        """Filesystem path to the python package directory. Resolved
        relative to the manifest's root, so a registry that points at
        a clone of the world repo continues to work after the world is
        moved."""
        return self.root / self.python_package


def load_manifest(path: str | Path) -> WorldManifest:
    """Load and validate a single world.toml. ``path`` may be the
    manifest file itself or the directory it lives in."""
    p = Path(path).expanduser().resolve()
    if p.is_dir():
        p = p / MANIFEST_FILENAME
    if not p.exists():
        raise ManifestError(f"no world manifest at {p}")
    try:
        data = _toml.loads(p.read_text())
    except _toml.TOMLDecodeError as e:
        raise ManifestError(f"{p}: invalid TOML ({e})") from e

    world = data.get("world")
    if not isinstance(world, dict):
        raise ManifestError(f"{p}: missing required [world] table")

    name = world.get("name")
    if not name:
        raise ManifestError(f"{p}: world.name is required")

    python_package = world.get("python_package") or name
    rust_crate = world.get("rust_crate")
    personas_field = world.get("personas_dir")
    personas_dir: Optional[Path] = None
    if personas_field:
        personas_dir = (p.parent / personas_field).resolve()

    default_tools = [
        t.get("name") if isinstance(t, dict) else t
        for t in world.get("default_tools", [])
    ]
    default_tools = [t for t in default_tools if isinstance(t, str)]

    default_personas = [
        t.get("name") if isinstance(t, dict) else t
        for t in world.get("default_personas", [])
    ]
    default_personas = [t for t in default_personas if isinstance(t, str)]

    resources = world.get("resources") or {}
    if not isinstance(resources, dict):
        raise ManifestError(f"{p}: world.resources must be a table")

    cli = world.get("cli") or {}
    if not isinstance(cli, dict):
        raise ManifestError(f"{p}: world.cli must be a table")

    default_user_model = world.get("default_user_model")
    default_agent_model = world.get("default_agent_model")

    return WorldManifest(
        name=str(name),
        root=p.parent,
        python_package=str(python_package),
        rust_crate=str(rust_crate) if rust_crate else None,
        personas_dir=personas_dir,
        default_tools=default_tools,
        default_personas=default_personas,
        default_user_model=str(default_user_model) if default_user_model else None,
        default_agent_model=str(default_agent_model) if default_agent_model else None,
        resources={
            k: (v if isinstance(v, dict) else {"value": v})
            for k, v in resources.items()
        },
        cli=cli,
        raw=world,
    )
