"""ensemble-train CLI: load a persona TOML, dispatch to a backend."""

from __future__ import annotations

from pathlib import Path

import click

from .backends import run_local, run_modal
from .spec import load_persona
from .stage_reporter import StageTrainingConfig, StageTrainingReporter


@click.command()
@click.argument("persona", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--backend",
    type=click.Choice(["modal", "skypilot", "local"]),
    default="modal",
    help="Where to run the training job.",
)
@click.option(
    "--output",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the output directory (defaults to checkpoints/<name>).",
)
def main(persona: Path, backend: str, output: Path | None) -> None:
    spec = load_persona(persona)
    if not spec.is_trainable:
        click.echo(
            f"persona {spec.name!r} has mode={spec.mode!r}; nothing to train."
        )
        return

    reporter: StageTrainingReporter | None = None
    stage_cfg = StageTrainingConfig.from_env()
    if stage_cfg is not None:
        try:
            hp = {}
            if spec.training:
                hp = {
                    **spec.training.dpo,
                    **spec.training.lora,
                    "base_model": spec.training.base_model,
                }
            reporter = StageTrainingReporter.start(
                stage_cfg,
                persona_name=spec.name,
                base_model=spec.training.base_model if spec.training else "",
                hyperparameters=hp,
            )
            click.echo(f"Stage:  {reporter.run_url}")
        except Exception as e:
            click.echo(f"warning: Stage training run create failed: {e}; continuing locally")
            reporter = None

    try:
        if backend == "local":
            result = run_local(spec, output_dir=output, stage_reporter=reporter)
        elif backend == "modal":
            result = run_modal(spec, output_dir=output)
        elif backend == "skypilot":
            from .backends.skypilot import run as run_skypilot
            result = run_skypilot(spec, output_dir=output)
        else:
            raise click.UsageError(f"unknown backend: {backend}")
    finally:
        if reporter is not None:
            artifact = result.pushed_to_hub or str(result.output_dir) if "result" in dir() else ""
            reporter.finish(artifact_uri=artifact)

    click.echo(
        f"trained {result.persona_name} on {result.base_model}; output at "
        f"{result.output_dir}. pushed: {result.pushed_to_hub or '(local only)'}"
    )


if __name__ == "__main__":
    main()
