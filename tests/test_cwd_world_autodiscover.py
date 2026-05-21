"""When a world.toml sits in the cwd, ensemble run discovers it and
imports the python package without an explicit `ensemble worlds add`."""

import importlib
import os
import sys
from pathlib import Path

from ensemble.cli_run import _autodiscover_cwd_world


def test_autodiscover_imports_cwd_world(tmp_path: Path, monkeypatch):
    pkg_name = "autodiscover_world_pkg"
    (tmp_path / "world.toml").write_text(
        f'[world]\nname = "autodiscover_test"\npython_package = "{pkg_name}"\n'
    )
    (tmp_path / f"{pkg_name}.py").write_text(
        "from ensemble import register_world, tool\n"
        "\n"
        "@tool\n"
        "def ping() -> str:\n"
        '    """Reply pong."""\n'
        "    return 'pong'\n"
        "\n"
        f"register_world(\"autodiscover_test\", tools=[ping])\n"
    )

    monkeypatch.chdir(tmp_path)
    # Ensure a stale import is not masking the test.
    sys.modules.pop(pkg_name, None)

    result = _autodiscover_cwd_world()
    assert result is not None
    name, root = result
    assert name == "autodiscover_test"
    assert root.resolve() == tmp_path.resolve()

    from ensemble.world import _WORLDS
    assert "autodiscover_test" in _WORLDS

    sys.modules.pop(pkg_name, None)


def test_autodiscover_returns_none_without_world_toml(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert _autodiscover_cwd_world() is None
