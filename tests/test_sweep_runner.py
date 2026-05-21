"""Sweep TOML parsing, cell expansion, and a small end-to-end run
against a scaffolded throwaway world."""

import json
from pathlib import Path

from ensemble.cli_sweep import _expand_cells, _load_sweep, main


def test_load_sweep_minimal(tmp_path: Path):
    (tmp_path / "sweep.toml").write_text(
        '[sweep]\nscenario = "x.smoke"\n[sweep.flags]\nbackend = ["mock"]\n'
    )
    cfg = _load_sweep(tmp_path / "sweep.toml")
    assert cfg.scenario == "x.smoke"
    assert cfg.flags == {"backend": ["mock"]}
    assert cfg.env == {}


def test_expand_cells_cartesian(tmp_path: Path):
    (tmp_path / "sweep.toml").write_text(
        """[sweep]
scenario = "x.smoke"
[sweep.flags]
backend = ["mock", "auto"]
[sweep.env]
SEED = ["1", "2", "3"]
"""
    )
    cfg = _load_sweep(tmp_path / "sweep.toml")
    cells = _expand_cells(cfg)
    assert len(cells) == 2 * 3


def test_sweep_run_end_to_end(tmp_path: Path, monkeypatch, capsys):
    # Lay down a tiny world the cli_run subprocess can import via
    # cwd autodiscovery, plus a minimal scenario that records a
    # cost so the meta.json captures something interesting.
    (tmp_path / "world.toml").write_text(
        '[world]\nname = "sweep_test"\npython_package = "sweep_test"\n'
    )
    (tmp_path / "sweep_test.py").write_text(
        "from ensemble import register_world, tool\n"
        "\n"
        "@tool\n"
        "def noop() -> str:\n"
        '    """no-op."""\n'
        "    return 'ok'\n"
        "\n"
        'register_world("sweep_test", tools=[noop])\n'
    )
    (tmp_path / "scenarios").mkdir()
    (tmp_path / "scenarios" / "__init__.py").write_text("from . import smoke\n")
    (tmp_path / "scenarios" / "smoke.py").write_text(
        'import sweep_test  # noqa: F401\n'
        "from ensemble import scenario\n"
        "\n"
        '@scenario("sweep_test.smoke", world="sweep_test")\n'
        "async def smoke(world):\n"
        '    world.spawn_agent(id="rep", tools=["noop"])\n'
        '    world.record_cost("usd", 0.001, actor="rep")\n'
        "    yield world.until(world.turn_count >= 1)\n"
        '    yield {"ok": 1.0}\n'
    )
    sweep_path = tmp_path / "sweep.toml"
    sweep_path.write_text(
        '[sweep]\n'
        'scenario = "sweep_test.smoke"\n'
        'world = "sweep_test"\n'
        'max_parallel = 2\n'
        f'package_dir = "{tmp_path}"\n'
        f'traces_dir = "{tmp_path / "out"}"\n'
        '\n'
        '[sweep.flags]\n'
        'backend = ["mock"]\n'
        '\n'
        '[sweep.env]\n'
        'TAG = ["a", "b"]\n'
    )

    monkeypatch.chdir(tmp_path)
    rc = main(["run", str(sweep_path)])
    assert rc == 0

    out_dir = tmp_path / "out"
    cells = sorted(p.name for p in out_dir.iterdir() if p.is_dir())
    assert cells == ["backend-mock__TAG-a", "backend-mock__TAG-b"]
    index_rows = [
        json.loads(line)
        for line in (out_dir / "index.jsonl").read_text().splitlines()
    ]
    assert len(index_rows) == 2
    for row in index_rows:
        assert row["scores"] == {"ok": 1.0}
        assert row["costs"]["usd"] == 0.001
        assert row["exit_code"] == 0
