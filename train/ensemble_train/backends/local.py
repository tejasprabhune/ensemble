"""Local backend: runs the trainer in-process. Useful for CPU smoke
tests against a tiny model; not realistic for full LoRA training."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..self_play import generate_preferences
from ..spec import PersonaSpec
from ..stage_reporter import StageTrainingReporter
from ..trainer import TrainedAdapter, train


def run(
    persona: PersonaSpec,
    output_dir: Optional[Path] = None,
    stage_reporter: Optional[StageTrainingReporter] = None,
) -> TrainedAdapter:
    out = output_dir or Path("checkpoints") / persona.name
    out.mkdir(parents=True, exist_ok=True)
    pairs = generate_preferences(persona)
    return train(persona, pairs, out, stage_reporter=stage_reporter)
