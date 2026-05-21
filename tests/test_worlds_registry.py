"""~/.ensemble/worlds.toml lifecycle."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ensemble import worlds_registry
from ensemble.world_manifest import ManifestError


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ENSEMBLE_HOME", str(tmp_path))
    yield tmp_path


def test_add_and_list_roundtrip(tmp_home):
    # Agora ships a real manifest at examples/agora.
    entry = worlds_registry.add_world("agora", Path("examples/agora"))
    assert entry.name == "agora"
    listed = worlds_registry.load_registry()
    assert "agora" in listed
    assert listed["agora"].path.name == "agora"


def test_add_rejects_name_mismatch(tmp_home):
    with pytest.raises(ManifestError, match="does not match manifest name"):
        worlds_registry.add_world("typo", Path("examples/agora"))


def test_remove_returns_false_when_missing(tmp_home):
    assert worlds_registry.remove_world("nope") is False


def test_remove_after_add(tmp_home):
    worlds_registry.add_world("agora", Path("examples/agora"))
    assert worlds_registry.remove_world("agora") is True
    assert "agora" not in worlds_registry.load_registry()


def test_cli_worlds_subcommands(tmp_home):
    """Drive the cli_worlds module end-to-end. Exercises argparse and
    the registry from the same surface ensemble run uses."""
    def cli(*argv):
        return subprocess.run(
            [sys.executable, "-m", "ensemble.cli_worlds", *argv],
            capture_output=True,
            text=True,
            env={
                **{k: v for k, v in __import__("os").environ.items()},
                "ENSEMBLE_HOME": str(tmp_home),
            },
        )

    r = cli("list")
    assert r.returncode == 0
    assert "no worlds registered" in r.stdout

    r = cli("add", "agora", "examples/agora")
    assert r.returncode == 0, r.stderr
    assert "registered agora" in r.stdout

    r = cli("list")
    assert "agora" in r.stdout

    r = cli("show", "agora")
    assert r.returncode == 0
    assert "default_tools" in r.stdout
    assert "issue_refund" in r.stdout

    r = cli("remove", "agora")
    assert r.returncode == 0

    r = cli("remove", "agora")
    assert r.returncode == 1
