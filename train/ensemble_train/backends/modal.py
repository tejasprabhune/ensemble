"""Modal backend: ships the trainer to Modal as a stub function. The
real deployment is left to the user; this file shows the shape and is
runnable as a dry-run that just prints the spec it would submit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ..self_play import generate_preferences
from ..spec import PersonaSpec
from ..trainer import TrainedAdapter

GPU = "A100-40GB"
TIMEOUT_S = 60 * 60  # 1 hour


def run(persona: PersonaSpec, output_dir: Optional[Path] = None) -> TrainedAdapter:
    try:
        import modal  # type: ignore[import-not-found]
    except ImportError:
        return _dry_run(persona, output_dir)

    stub = modal.App(f"ensemble-persona-{persona.name}")
    image = (
        modal.Image.debian_slim()
        .pip_install("torch", "transformers", "trl", "peft", "datasets", "accelerate", "huggingface_hub")
    )

    @stub.function(image=image, gpu=GPU, timeout=TIMEOUT_S)
    def _train_remote(spec_json: str) -> dict:
        import json as _json
        from ..spec import PersonaSpec as _PS, TrainingHyperparams as _TH
        from ..trainer import train as _train
        from ..self_play import generate_preferences as _gen

        raw = _json.loads(spec_json)
        tr_raw = raw.get("training") or {}
        spec = _PS(
            name=raw["name"],
            mode=raw["mode"],
            description=raw["description"],
            style=raw["style"],
            demographics=raw["demographics"],
            hidden_state_schema=raw["hidden_state_schema"],
            system_prompt_template=raw["system_prompt_template"],
            training=_TH(**tr_raw) if tr_raw else None,
        )
        pairs = _gen(spec)
        out = Path("/tmp/persona-out")
        out.mkdir(parents=True, exist_ok=True)
        result = _train(spec, pairs, out)
        return {
            "persona_name": result.persona_name,
            "base_model": result.base_model,
            "pushed_to_hub": result.pushed_to_hub,
        }

    spec_json = _spec_to_json(persona)
    with stub.run():
        info = _train_remote.remote(spec_json)
    return TrainedAdapter(
        persona_name=info["persona_name"],
        base_model=info["base_model"],
        output_dir=output_dir or Path("checkpoints") / persona.name,
        pushed_to_hub=info.get("pushed_to_hub"),
    )


def _dry_run(persona: PersonaSpec, output_dir: Optional[Path]) -> TrainedAdapter:
    out = output_dir or Path("checkpoints") / persona.name
    out.mkdir(parents=True, exist_ok=True)
    pairs = generate_preferences(persona)
    print(
        f"[modal dry-run] would train {persona.name} on {persona.training.base_model if persona.training else '?'} "
        f"with {len(pairs)} preference pairs on {GPU}."
    )
    (out / "DRY_RUN.json").write_text(json.dumps({"pairs": len(pairs)}))
    return TrainedAdapter(
        persona_name=persona.name,
        base_model=persona.training.base_model if persona.training else "",
        output_dir=out,
    )


def _spec_to_json(persona: PersonaSpec) -> str:
    payload = {
        "name": persona.name,
        "mode": persona.mode,
        "description": persona.description,
        "style": persona.style,
        "demographics": persona.demographics,
        "hidden_state_schema": persona.hidden_state_schema,
        "system_prompt_template": persona.system_prompt_template,
        "training": persona.training.__dict__ if persona.training else None,
    }
    return json.dumps(payload)
