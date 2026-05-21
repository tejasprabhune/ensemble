"""Persona TOML loading.

A persona lives next to its world. The world's python package registers
its personas directory at import time via ``register_personas_dir``;
``spawn_user(persona="...")`` then looks up the TOML, extracts the
system prompt template and the default hidden state, and threads both
into the native side so the user actor's backend is wrapped in a
PromptedPersona.

The schema is the one documented in ``examples/agora/personas/*.toml``:

    [persona]
    name = "frustrated_power_user"
    mode = "trained" | "prompted"

    [persona.style]                       # arbitrary kv pairs
    [persona.demographics]                # arbitrary kv pairs
    [persona.hidden_state.schema]
    field = { type = "string", default = "..." }

    [persona.system_prompt]
    template = "..."
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

if sys.version_info >= (3, 11):
    import tomllib as _toml
else:
    import tomli as _toml  # type: ignore[import-not-found]


_PERSONA_DIRS: Dict[str, Path] = {}


def register_personas_dir(world_name: str, path: str | Path) -> None:
    """Register where to find this world's persona TOMLs. Worlds call
    this from their package's ``__init__`` so scenarios can refer to
    personas by short name."""
    _PERSONA_DIRS[world_name] = Path(path)


def personas_dir(world_name: str) -> Optional[Path]:
    return _PERSONA_DIRS.get(world_name)


class PersonaResolver:
    """Resolve a persona name to a system prompt and initial hidden
    state for a given world. Bound to the world's name so the
    look-up is unambiguous."""

    def __init__(self, world_name: str) -> None:
        self.world_name = world_name
        self.dir = personas_dir(world_name)

    def resolve(
        self,
        name: str,
        hidden_overrides: Optional[Dict[str, Any]] = None,
    ) -> Optional["PersonaSpec"]:
        if self.dir is None:
            return None
        candidate = self.dir / f"{name}.toml"
        if not candidate.exists():
            return None
        return load_persona(candidate, hidden_overrides=hidden_overrides)


class PersonaSpec:
    """Parsed persona ready to thread into native spawn_user."""

    def __init__(
        self,
        name: str,
        system_prompt: str,
        hidden_state: Dict[str, Any],
        raw: Dict[str, Any],
        mode: str = "prompted",
        adapter_name: Optional[str] = None,
        serve_url: Optional[str] = None,
    ) -> None:
        self.name = name
        self.system_prompt = system_prompt
        self.hidden_state = hidden_state
        self.raw = raw
        self.mode = mode
        self.adapter_name = adapter_name
        self.serve_url = serve_url

    @property
    def is_trained(self) -> bool:
        """A persona whose inference should route through a trained
        adapter rather than the world's default backend. Both
        ``mode = "trained"`` and an ``adapter_name`` from
        ``[persona.training]`` are required; a persona missing one
        falls back to prompted inference."""
        return self.mode == "trained" and bool(self.adapter_name)


def load_persona(
    path: str | Path,
    hidden_overrides: Optional[Dict[str, Any]] = None,
) -> PersonaSpec:
    data = _toml.loads(Path(path).read_text())
    persona = data.get("persona") or {}
    name = persona.get("name") or Path(path).stem
    mode = persona.get("mode", "prompted")

    template = (
        persona.get("system_prompt", {}).get("template")
        or _fallback_template(persona)
    )

    schema = persona.get("hidden_state", {}).get("schema", {})
    hidden_state: Dict[str, Any] = {}
    for key, defn in schema.items():
        if isinstance(defn, dict) and "default" in defn:
            hidden_state[key] = defn["default"]
    if hidden_overrides:
        hidden_state.update({k: v for k, v in hidden_overrides.items() if v is not None})

    training = persona.get("training") or {}
    adapter_name = training.get("adapter_name") if isinstance(training, dict) else None
    serve_url = training.get("serve_url") if isinstance(training, dict) else None

    return PersonaSpec(
        name=name,
        system_prompt=template.strip(),
        hidden_state=hidden_state,
        raw=persona,
        mode=mode,
        adapter_name=adapter_name,
        serve_url=serve_url,
    )


def _fallback_template(persona: Dict[str, Any]) -> str:
    desc = persona.get("description") or "You are a simulated user."
    style = persona.get("style") or {}
    style_bits = ", ".join(f"{k}={v}" for k, v in style.items())
    if style_bits:
        return f"{desc} Style: {style_bits}."
    return desc
