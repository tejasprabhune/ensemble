"""ensemble models list: prints backends, key status, and model ids
from the runtime crate's pricing table."""

from ensemble.cli_models import main


def test_models_list_prints_each_backend(capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ENSEMBLE_VLLM_BASE_URL", raising=False)

    rc = main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    for label in ("[anthropic]", "[openai]", "[vllm]", "[mock]"):
        assert label in out
    assert "claude-sonnet-4-5" in out
    assert "gpt-4o" in out
    assert "ANTHROPIC_API_KEY: not set" in out


def test_models_list_shows_key_set_state(capsys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-1234567890")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ENSEMBLE_VLLM_BASE_URL", raising=False)

    rc = main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ANTHROPIC_API_KEY: set (sk-tes...)" in out
    assert "OPENAI_API_KEY: not set" in out
