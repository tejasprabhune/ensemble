"""SkyPilot backend: emits a SkyPilot YAML next to the persona and
launches it via the `sky` CLI. Falls back to a dry-run that just writes
the YAML when SkyPilot isn't installed."""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Optional

from ..spec import PersonaSpec
from ..trainer import TrainedAdapter


def render_task_yaml(persona: PersonaSpec, output_dir: Path) -> str:
    base = persona.training.base_model if persona.training else ""
    return textwrap.dedent(
        f"""
        name: ensemble-persona-{persona.name}

        resources:
          accelerators: A100:1
          disk_size: 200

        workdir: .

        envs:
          PERSONA_NAME: {persona.name}
          BASE_MODEL: {base}
          HF_USERNAME: ""

        setup: |
          pip install --upgrade pip
          pip install torch transformers trl peft datasets accelerate huggingface_hub
          pip install -e ./train

        run: |
          ensemble-train {persona.source_path} --backend local --output {output_dir}
        """
    ).strip() + "\n"


def run(persona: PersonaSpec, output_dir: Optional[Path] = None) -> TrainedAdapter:
    out = output_dir or Path("checkpoints") / persona.name
    out.mkdir(parents=True, exist_ok=True)
    yaml_path = out / f"{persona.name}.sky.yaml"
    yaml_path.write_text(render_task_yaml(persona, out))

    sky = shutil.which("sky")
    if sky is None:
        print(
            f"[skypilot dry-run] wrote {yaml_path}. install skypilot and run "
            f"`sky launch {yaml_path}` to dispatch."
        )
    else:
        subprocess.check_call([sky, "launch", "-y", str(yaml_path)])

    return TrainedAdapter(
        persona_name=persona.name,
        base_model=persona.training.base_model if persona.training else "",
        output_dir=out,
    )
