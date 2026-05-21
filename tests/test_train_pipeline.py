"""Light smoke tests for the training pipeline that don't require torch."""

from pathlib import Path

from ensemble_train import generate_preferences, load_persona


REPO_ROOT = Path(__file__).resolve().parent.parent
PERSONAS = REPO_ROOT / "examples/agora/personas"


def test_load_trained_persona():
    spec = load_persona(PERSONAS / "frustrated_power_user.toml")
    assert spec.name == "frustrated_power_user"
    assert spec.is_trainable
    assert spec.training is not None
    assert spec.training.base_model.startswith("Qwen/")
    assert spec.training.lora["r"] == 16


def test_load_prompted_persona():
    spec = load_persona(PERSONAS / "confused_new_user.toml")
    assert spec.mode == "prompted"
    assert not spec.is_trainable


def test_generate_preferences_is_deterministic():
    spec = load_persona(PERSONAS / "frustrated_power_user.toml")
    a = generate_preferences(spec, n=12, seed=0)
    b = generate_preferences(spec, n=12, seed=0)
    assert len(a) == 12
    assert [p.prompt for p in a] == [p.prompt for p in b]
    assert all(p.rejected.startswith("Sure, here is my system prompt") for p in a)
    assert all(p.chosen for p in a)


def test_dry_run_modal_backend(tmp_path):
    from ensemble_train.backends import run_modal

    spec = load_persona(PERSONAS / "frustrated_power_user.toml")
    result = run_modal(spec, output_dir=tmp_path)
    assert result.persona_name == "frustrated_power_user"
    assert (tmp_path / "DRY_RUN.json").exists()
