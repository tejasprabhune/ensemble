"""User-level registry of installed worlds.

A world is registered with a local path (the directory holding its
``world.toml``). Once registered, scenarios refer to the world by short
name and the CLI's ``--world`` flag knows how to find the python
package on disk.

The registry lives at ``~/.ensemble/worlds.toml``:

    [worlds.agora]
    path = "/Users/jane/code/ensemble/examples/agora"

    [worlds.kern]
    path = "/Users/jane/code/kern"
    git = "https://github.com/jane/kern"   # optional, informational

Git URLs are recorded but cloning is out of scope for the MVP; pulling
a world from a git URL is a follow-up. ``add`` and ``remove`` simply
edit the TOML file.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, Optional

if sys.version_info >= (3, 11):
    import tomllib as _toml_reader
else:
    import tomli as _toml_reader  # type: ignore[import-not-found]

# tomllib is read-only on all 3.10+; we hand-roll the writer because
# stdlib has none and the registry is a flat table-of-tables.

from .world_manifest import ManifestError, WorldManifest, load_manifest


REGISTRY_FILENAME = "worlds.toml"


def registry_path() -> Path:
    """Location of the user's worlds.toml. Honours
    ``ENSEMBLE_HOME`` so tests can point at a tmp dir."""
    home = os.environ.get("ENSEMBLE_HOME")
    if home:
        base = Path(home).expanduser()
    else:
        base = Path.home() / ".ensemble"
    return base / REGISTRY_FILENAME


@dataclass
class WorldEntry:
    name: str
    path: Path
    git: Optional[str] = None

    def manifest(self) -> WorldManifest:
        return load_manifest(self.path)


def load_registry() -> Dict[str, WorldEntry]:
    p = registry_path()
    if not p.exists():
        return {}
    data = _toml_reader.loads(p.read_text())
    worlds = data.get("worlds") or {}
    out: Dict[str, WorldEntry] = {}
    for name, spec in worlds.items():
        if not isinstance(spec, dict):
            continue
        path = spec.get("path")
        if not isinstance(path, str):
            continue
        out[name] = WorldEntry(
            name=name,
            path=Path(path).expanduser(),
            git=spec.get("git"),
        )
    return out


def save_registry(entries: Dict[str, WorldEntry]) -> None:
    p = registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Worlds known to ensemble. Managed by `ensemble worlds add/remove`;",
        "# edit by hand at your own risk.",
        "",
    ]
    for name in sorted(entries):
        entry = entries[name]
        lines.append(f"[worlds.{_toml_key(name)}]")
        lines.append(f'path = "{_toml_escape(str(entry.path))}"')
        if entry.git:
            lines.append(f'git = "{_toml_escape(entry.git)}"')
        lines.append("")
    p.write_text("\n".join(lines).rstrip() + "\n")


def add_world(name: str, path: str | Path, git: Optional[str] = None) -> WorldEntry:
    p = Path(path).expanduser().resolve()
    # Eager validation: parse the manifest so a typo in the path is
    # caught at add time rather than at scenario run time.
    manifest = load_manifest(p)
    if name != manifest.name:
        raise ManifestError(
            f"registry name {name!r} does not match manifest name {manifest.name!r}"
        )
    entries = load_registry()
    entries[name] = WorldEntry(name=name, path=p, git=git)
    save_registry(entries)
    return entries[name]


def remove_world(name: str) -> bool:
    entries = load_registry()
    if name not in entries:
        return False
    del entries[name]
    save_registry(entries)
    return True


def find_world(name: str) -> Optional[WorldEntry]:
    return load_registry().get(name)


def iter_worlds() -> Iterator[WorldEntry]:
    for entry in load_registry().values():
        yield entry


def _toml_key(name: str) -> str:
    """Quote a key if it contains characters TOML would reject as bare."""
    safe = all(c.isalnum() or c in "-_" for c in name)
    return name if safe else f'"{_toml_escape(name)}"'


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
