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
    # Plank ships a real manifest at examples/plank.
    entry = worlds_registry.add_world("plank", Path("examples/plank"))
    assert entry.name == "plank"
    listed = worlds_registry.load_registry()
    assert "plank" in listed
    assert listed["plank"].path.name == "plank"


def test_add_rejects_name_mismatch(tmp_home):
    with pytest.raises(ManifestError, match="does not match manifest name"):
        worlds_registry.add_world("typo", Path("examples/plank"))


def test_remove_returns_false_when_missing(tmp_home):
    assert worlds_registry.remove_world("nope") is False


def test_remove_after_add(tmp_home):
    worlds_registry.add_world("plank", Path("examples/plank"))
    assert worlds_registry.remove_world("plank") is True
    assert "plank" not in worlds_registry.load_registry()


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

    r = cli("add", "plank", "examples/plank")
    assert r.returncode == 0, r.stderr
    assert "registered plank" in r.stdout

    r = cli("list")
    assert "plank" in r.stdout

    r = cli("show", "plank")
    assert r.returncode == 0
    assert "default_tools" in r.stdout
    assert "issue_refund" in r.stdout

    r = cli("remove", "plank")
    assert r.returncode == 0

    r = cli("remove", "plank")
    assert r.returncode == 1
