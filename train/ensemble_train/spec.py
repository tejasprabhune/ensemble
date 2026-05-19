"""Persona spec loader. Reads the TOML files produced under
`examples/<world>/personas/`."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

if sys.version_info >= (3, 11):
    import tomllib as toml_reader  # type: ignore[import-not-found]
else:
    import tomli as toml_reader  # type: ignore[import-not-found]


@dataclass
class TrainingHyperparams:
    base_model: str = "Qwen/Qwen2.5-7B-Instruct"
    backend: str = "modal"
    dataset: str = "spec_only"
    hf_namespace_env: str = "HF_USERNAME"
    adapter_name: Optional[str] = None
    lora: Dict[str, Any] = field(default_factory=dict)
    dpo: Dict[str, Any] = field(default_factory=dict)
    self_play: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PersonaSpec:
    name: str
    mode: str  # "trained" or "prompted"
    description: str
    style: Dict[str, Any] = field(default_factory=dict)
    demographics: Dict[str, Any] = field(default_factory=dict)
    hidden_state_schema: Dict[str, Any] = field(default_factory=dict)
    system_prompt_template: str = ""
    training: Optional[TrainingHyperparams] = None
    source_path: Optional[Path] = None

    @property
    def is_trainable(self) -> bool:
        return self.mode == "trained" and self.training is not None


def load_persona(path: str | Path) -> PersonaSpec:
    p = Path(path)
    raw = toml_reader.loads(p.read_text())
    persona = raw.get("persona", {})
    if not persona:
        raise ValueError(f"persona TOML at {p} has no [persona] table")
    name = persona.get("name") or p.stem
    mode = persona.get("mode", "prompted")
    description = persona.get("description", "")
    style = persona.get("style", {})
    demographics = persona.get("demographics", {})
    hidden = persona.get("hidden_state", {}).get("schema", {})
    sys_prompt = persona.get("system_prompt", {}).get("template", "")

    training: Optional[TrainingHyperparams] = None
    tr_raw = persona.get("training")
    if tr_raw is not None:
        training = TrainingHyperparams(
            base_model=tr_raw.get("base_model", "Qwen/Qwen2.5-7B-Instruct"),
            backend=tr_raw.get("backend", "modal"),
            dataset=tr_raw.get("dataset", "spec_only"),
            hf_namespace_env=tr_raw.get("hf_namespace_env", "HF_USERNAME"),
            adapter_name=tr_raw.get("adapter_name"),
            lora=tr_raw.get("lora", {}),
            dpo=tr_raw.get("dpo", {}),
            self_play=tr_raw.get("self_play", {}),
        )
        if mode != "trained":
            mode = "trained"

    return PersonaSpec(
        name=name,
        mode=mode,
        description=description,
        style=style,
        demographics=demographics,
        hidden_state_schema=hidden,
        system_prompt_template=sys_prompt,
        training=training,
        source_path=p,
    )
