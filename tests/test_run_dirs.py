"""Per-run trace directories, runs.jsonl index, and the flat-path
symlink for back-compat."""

import json
from pathlib import Path

from ensemble.cli_run import main


def test_run_writes_dir_meta_and_index(tmp_path: Path, monkeypatch):
    pkg = "rundirs_world_pkg"
    (tmp_path / "world.toml").write_text(
        f'[world]\nname = "rundirs"\npython_package = "{pkg}"\n'
    )
    (tmp_path / f"{pkg}.py").write_text(
        "from ensemble import register_world, tool\n"
        "\n"
        "@tool\n"
        "def noop() -> str:\n"
        '    """no-op."""\n'
        "    return 'ok'\n"
        "\n"
        f'register_world("rundirs", tools=[noop])\n'
    )
    (tmp_path / "scenarios").mkdir()
    (tmp_path / "scenarios" / "__init__.py").write_text("from . import smoke\n")
    (tmp_path / "scenarios" / "smoke.py").write_text(
        f'import {pkg}  # noqa: F401\n'
        "from ensemble import scenario\n"
        "\n"
        '@scenario("rundirs.smoke", world="rundirs")\n'
        "async def smoke(world):\n"
        '    world.spawn_agent(id="rep", tools=["noop"])\n'
        "    yield world.until(world.turn_count >= 1)\n"
        '    yield {"ok": 1.0}\n'
    )

    monkeypatch.chdir(tmp_path)
    traces_dir = tmp_path / "traces"
    rc = main(["--scenario", "rundirs.smoke", "--traces-dir", str(traces_dir)])
    assert rc == 0

    run_dirs = sorted(p for p in traces_dir.iterdir() if p.is_dir())
    assert len(run_dirs) == 1
    rd = run_dirs[0]
    assert (rd / "trace.jsonl").is_file()
    meta = json.loads((rd / "meta.json").read_text())
    assert meta["scenario"] == "rundirs.smoke"
    assert meta["scores"] == {"ok": 1.0}
    assert meta["run_id"] == rd.name

    index = (traces_dir / "runs.jsonl").read_text().splitlines()
    assert len(index) == 1
    assert json.loads(index[0])["run_id"] == rd.name

    # Flat symlink points at the latest run's trace.
    flat = traces_dir / "rundirs_smoke.jsonl"
    assert flat.exists()
    assert flat.is_symlink() or flat.is_file()


def test_two_runs_append_to_index(tmp_path: Path, monkeypatch):
    # Reuses whichever world/scenario the previous test (within the
    # same session) registered. The scenario registry is module-level
    # in ensemble.scenario; we just need to call main twice and verify
    # the index gains a row per call.
    from ensemble.scenario import _REGISTRY
    name = next(iter(_REGISTRY))
    monkeypatch.chdir(tmp_path)
    traces_dir = tmp_path / "traces"
    for _ in range(2):
        rc = main(["--scenario", name, "--traces-dir", str(traces_dir)])
        assert rc == 0
        import time as _t
        _t.sleep(1.1)

    index_rows = (traces_dir / "runs.jsonl").read_text().splitlines()
    assert len(index_rows) == 2
    ids = {json.loads(r)["run_id"] for r in index_rows}
    assert len(ids) == 2
