"""UUID7 run identity: stability, format, and CLI output."""

import json
import re
from pathlib import Path

import pytest

from ensemble import World
from ensemble.cli_run import main

UUID7_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
)


def test_run_id_is_uuid7_format():
    w = World("noop")
    assert UUID7_RE.match(w.run_id), f"run_id {w.run_id!r} is not a UUID7"


def test_run_id_stable_across_lifetime():
    w = World("noop")
    assert w.run_id == w.run_id


def test_two_worlds_get_distinct_run_ids():
    a = World("noop")
    b = World("noop")
    assert a.run_id != b.run_id


def test_run_id_stability(tmp_path: Path, monkeypatch):
    """The run_id in meta.json equals the trace directory name (both UUID7)."""
    pkg = "runid_stab_pkg"
    world_name = "runid_stab"
    scenario_name = "runid_stab.stab_check"
    (tmp_path / "world.toml").write_text(
        f'[world]\nname = "{world_name}"\npython_package = "{pkg}"\n'
    )
    (tmp_path / f"{pkg}.py").write_text(
        f'from ensemble import register_world\nregister_world("{world_name}")\n'
    )
    (tmp_path / "scenarios").mkdir()
    (tmp_path / "scenarios" / "__init__.py").write_text("from . import stab_check\n")
    # Unique file name avoids collision with "scenarios.smoke" cached from
    # other tests that share the global sys.modules.
    (tmp_path / "scenarios" / "stab_check.py").write_text(
        f'import {pkg}  # noqa: F401\n'
        "from ensemble import scenario\n"
        "\n"
        f'@scenario("{scenario_name}", world="{world_name}")\n'
        "async def stab_check(world):\n"
        "    yield world.until(world.turn_count >= 1)\n"
        '    yield {"ok": 1.0}\n'
    )
    monkeypatch.chdir(tmp_path)
    traces_dir = tmp_path / "traces"

    rc = main(["--scenario", scenario_name, "--traces-dir", str(traces_dir)])
    assert rc == 0

    run_dirs = [p for p in traces_dir.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1
    rd = run_dirs[0]
    meta = json.loads((rd / "meta.json").read_text())

    assert meta["run_id"] == rd.name, "meta run_id must match directory name"
    assert UUID7_RE.match(rd.name), f"directory name {rd.name!r} is not a UUID7"


def test_run_id_in_cli_output(tmp_path: Path, monkeypatch, capsys):
    """ensemble run prints 'Run id: <uuid7>' to stderr before the scenario starts."""
    pkg = "runid_cli_pkg"
    world_name = "runid_cli"
    scenario_name = "runid_cli.cli_output"
    (tmp_path / "world.toml").write_text(
        f'[world]\nname = "{world_name}"\npython_package = "{pkg}"\n'
    )
    (tmp_path / f"{pkg}.py").write_text(
        f'from ensemble import register_world\nregister_world("{world_name}")\n'
    )
    (tmp_path / "scenarios").mkdir()
    (tmp_path / "scenarios" / "__init__.py").write_text("from . import cli_output\n")
    # Use a unique file name to avoid the module cache collision with
    # "scenarios.smoke" from test_run_id_stability.
    (tmp_path / "scenarios" / "cli_output.py").write_text(
        f'import {pkg}  # noqa: F401\n'
        "from ensemble import scenario\n"
        "\n"
        f'@scenario("{scenario_name}", world="{world_name}")\n'
        "async def cli_output(world):\n"
        "    yield world.until(world.turn_count >= 1)\n"
        '    yield {"ok": 1.0}\n'
    )
    monkeypatch.chdir(tmp_path)
    traces_dir = tmp_path / "traces"

    rc = main(["--scenario", scenario_name, "--traces-dir", str(traces_dir)])
    assert rc == 0

    captured = capsys.readouterr()
    run_id_line = next(
        (l for l in captured.err.splitlines() if l.startswith("Run id:")),
        None,
    )
    assert run_id_line is not None, f"'Run id:' not found in stderr:\n{captured.err}"
    run_id = run_id_line.split(":", 1)[1].strip()
    assert UUID7_RE.match(run_id), f"stderr run_id {run_id!r} is not a UUID7"

    run_dirs = [p for p in traces_dir.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1
    meta = json.loads((run_dirs[0] / "meta.json").read_text())
    assert meta["run_id"] == run_id
