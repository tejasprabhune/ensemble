"""Tiny dotenv reader: parse the supported subset and skip the rest."""

import os
from pathlib import Path

from ensemble.env import load_dotenv


def test_load_dotenv_sets_vars(tmp_path, monkeypatch):
    monkeypatch.delenv("FOO_FROM_DOTENV", raising=False)
    monkeypatch.delenv("BAR_FROM_DOTENV", raising=False)
    monkeypatch.delenv("ALREADY_SET", raising=False)
    monkeypatch.setenv("ALREADY_SET", "preserved")

    p = tmp_path / ".env"
    p.write_text(
        """
        # a comment
        FOO_FROM_DOTENV=hello
        export BAR_FROM_DOTENV="quoted value"
        BLANK_KEY=
        ALREADY_SET=overridden
        SOMETHING_WITH_HASH=value # inline comment
        """
    )
    assert load_dotenv(p) is True
    assert os.environ["FOO_FROM_DOTENV"] == "hello"
    assert os.environ["BAR_FROM_DOTENV"] == "quoted value"
    assert os.environ["BLANK_KEY"] == ""
    assert os.environ["ALREADY_SET"] == "preserved"
    assert os.environ["SOMETHING_WITH_HASH"] == "value"


def test_load_dotenv_returns_false_when_missing(tmp_path):
    assert load_dotenv(tmp_path / "does_not_exist") is False


def test_world_announces_backend(monkeypatch, capsys):
    from ensemble import World

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    world = World("noop", backend="mock", dotenv=False)
    err = capsys.readouterr().err
    assert "backend=mock" in err
    trace = world.trace()
    assert any(
        e["payload"].get("kind") == "system"
        and "backend=mock" in e["payload"].get("note", "")
        for e in trace
    )
