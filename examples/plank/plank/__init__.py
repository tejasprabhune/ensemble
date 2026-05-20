"""Plank: a worked example world for Ensemble.

Importing this package registers Plank's personas directory with
ensemble so scenarios can refer to personas by short name
(`spawn_user(persona="frustrated_power_user")`). The Rust side of the
world is bundled into the `ensemble` extension at build time; that
registration runs unconditionally at extension import.
"""

from pathlib import Path

from ensemble import register_personas_dir

PERSONAS_DIR = Path(__file__).resolve().parent.parent / "personas"
register_personas_dir("plank", PERSONAS_DIR)

__all__ = ["PERSONAS_DIR"]
