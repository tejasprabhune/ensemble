"""ensemble-train CLI: load a persona TOML, dispatch to a backend."""

from __future__ import annotations

from pathlib import Path

import click

from .backends import run_local, run_modal
from .spec import load_persona


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
    if backend == "local":
        result = run_local(spec, output_dir=output)
    elif backend == "modal":
        result = run_modal(spec, output_dir=output)
    elif backend == "skypilot":
        from .backends.skypilot import run as run_skypilot
        result = run_skypilot(spec, output_dir=output)
    else:
        raise click.UsageError(f"unknown backend: {backend}")
    click.echo(
        f"trained {result.persona_name} on {result.base_model}; output at "
        f"{result.output_dir}. pushed: {result.pushed_to_hub or '(local only)'}"
    )


if __name__ == "__main__":
    main()
