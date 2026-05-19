from pathlib import Path

from ensemble_train.backends.skypilot import render_task_yaml, run
from ensemble_train import load_persona


REPO_ROOT = Path(__file__).resolve().parent.parent
PERSONA = REPO_ROOT / "examples/plank/personas/frustrated_power_user.toml"


def test_render_task_yaml():
    spec = load_persona(PERSONA)
    yaml = render_task_yaml(spec, Path("/tmp/out"))
    assert "ensemble-persona-frustrated_power_user" in yaml
    assert "accelerators: A100:1" in yaml
    assert "Qwen/Qwen2.5-7B-Instruct" in yaml


def test_dry_run_writes_yaml(tmp_path):
    spec = load_persona(PERSONA)
    result = run(spec, output_dir=tmp_path)
    assert result.persona_name == "frustrated_power_user"
    yaml = tmp_path / "frustrated_power_user.sky.yaml"
    assert yaml.exists()
    assert "sky launch" not in yaml.read_text()  # sanity: yaml itself
