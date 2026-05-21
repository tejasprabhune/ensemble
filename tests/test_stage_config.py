"""Tests for Stage config precedence, login, and whoami."""

from __future__ import annotations

import http.server
import json
import os
import stat
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _MockHTTPHandler(http.server.BaseHTTPRequestHandler):
    """Simple handler that replies with a pre-configured JSON body."""

    def do_GET(self):
        response = self.server._responses.get(self.path, {"error": "not found"})
        body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.do_GET()

    def log_message(self, fmt, *args):
        pass


def _start_mock_server(responses: dict) -> tuple[http.server.HTTPServer, str]:
    """Start a local HTTP server; return (server, base_url)."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _MockHTTPHandler)
    server._responses = responses
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# test_stage_config_precedence
# ---------------------------------------------------------------------------

def test_stage_config_explicit_beats_env(monkeypatch, tmp_path):
    """Explicit Stage() overrides env vars."""
    from ensemble.stage import Stage

    monkeypatch.setenv("ENSEMBLE_STAGE_API_KEY", "env_key")
    monkeypatch.setenv("ENSEMBLE_STAGE_PROJECT", "env_org/env_proj")

    stage = Stage(project="my_org/my_proj", api_key="explicit_key")
    cfg = Stage.resolve(explicit=stage)
    assert cfg is not None
    assert cfg.api_key == "explicit_key"
    assert cfg.project == "my_org/my_proj"


def test_stage_config_env_beats_toml(monkeypatch, tmp_path):
    """Env vars beat TOML files."""
    from ensemble.stage import Stage

    monkeypatch.setenv("ENSEMBLE_STAGE_API_KEY", "env_api_key")
    monkeypatch.setenv("ENSEMBLE_STAGE_PROJECT", "env_org/env_proj")

    # Write a TOML that would supply different values
    creds_dir = tmp_path / ".ensemble"
    creds_dir.mkdir()
    (creds_dir / "stage.toml").write_text('[credentials]\napi_key = "toml_key"\n')
    proj_toml = tmp_path / ".stage.toml"
    proj_toml.write_text('[stage]\nproject = "toml_org/toml_proj"\n')

    import ensemble.stage as _stage_mod
    monkeypatch.setattr(_stage_mod, "_CREDS_PATH", creds_dir / "stage.toml")
    monkeypatch.setattr(_stage_mod, "_PROJECT_TOML", proj_toml)

    cfg = Stage.resolve()
    assert cfg is not None
    assert cfg.api_key == "env_api_key"
    assert cfg.project == "env_org/env_proj"


def test_stage_config_toml_used_when_no_env(monkeypatch, tmp_path):
    """TOML files are used when no env vars are set."""
    from ensemble.stage import Stage

    monkeypatch.delenv("ENSEMBLE_STAGE_API_KEY", raising=False)
    monkeypatch.delenv("ENSEMBLE_STAGE_PROJECT", raising=False)
    monkeypatch.delenv("ENSEMBLE_STAGE_BASE_URL", raising=False)
    monkeypatch.delenv("ENSEMBLE_STAGE_ENABLED", raising=False)

    creds_dir = tmp_path / ".ensemble"
    creds_dir.mkdir()
    creds_file = creds_dir / "stage.toml"
    creds_file.write_text('[credentials]\napi_key = "toml_key"\n')
    proj_toml = tmp_path / ".stage.toml"
    proj_toml.write_text('[stage]\nproject = "toml_org/toml_proj"\n')

    import ensemble.stage as _stage_mod
    monkeypatch.setattr(_stage_mod, "_CREDS_PATH", creds_file)
    monkeypatch.setattr(_stage_mod, "_PROJECT_TOML", proj_toml)

    cfg = Stage.resolve()
    assert cfg is not None
    assert cfg.api_key == "toml_key"
    assert cfg.project == "toml_org/toml_proj"


def test_stage_disabled_by_env(monkeypatch, tmp_path):
    """ENSEMBLE_STAGE_ENABLED=0 disables Stage even when fully configured."""
    from ensemble.stage import Stage

    monkeypatch.setenv("ENSEMBLE_STAGE_API_KEY", "env_key")
    monkeypatch.setenv("ENSEMBLE_STAGE_PROJECT", "org/proj")
    monkeypatch.setenv("ENSEMBLE_STAGE_ENABLED", "0")

    stage = Stage(project="org/proj", api_key="explicit_key")
    assert Stage.resolve(explicit=stage) is None
    assert Stage.resolve() is None


def test_stage_config_none_when_incomplete(monkeypatch, tmp_path):
    """No config returns None when env vars and TOML are absent."""
    from ensemble.stage import Stage

    monkeypatch.delenv("ENSEMBLE_STAGE_API_KEY", raising=False)
    monkeypatch.delenv("ENSEMBLE_STAGE_PROJECT", raising=False)
    monkeypatch.delenv("ENSEMBLE_STAGE_BASE_URL", raising=False)
    monkeypatch.delenv("ENSEMBLE_STAGE_ENABLED", raising=False)

    import ensemble.stage as _stage_mod
    monkeypatch.setattr(_stage_mod, "_CREDS_PATH", tmp_path / "no_creds.toml")
    monkeypatch.setattr(_stage_mod, "_PROJECT_TOML", tmp_path / "no_proj.toml")

    assert Stage.resolve() is None


# ---------------------------------------------------------------------------
# test_stage_login_writes_credentials
# ---------------------------------------------------------------------------

def test_stage_login_writes_credentials(monkeypatch, tmp_path):
    """Login callback writes credentials with 0600 permissions."""
    from ensemble.stage import write_credentials, PROD_BASE_URL

    creds_path = tmp_path / ".ensemble" / "stage.toml"

    import ensemble.stage as _stage_mod
    monkeypatch.setattr(_stage_mod, "_CREDS_PATH", creds_path)

    write_credentials("test_key_abc", base_url=PROD_BASE_URL, user_login="testuser")

    assert creds_path.exists()
    content = creds_path.read_text()
    assert "test_key_abc" in content
    assert "testuser" in content

    file_stat = creds_path.stat()
    mode = file_stat.st_mode & 0o777
    assert mode == 0o600, f"Expected 0600 permissions, got {oct(mode)}"


def test_stage_login_flow_writes_key(monkeypatch, tmp_path):
    """Simulate OAuth callback flow writing api_key to credentials file."""
    import threading
    import http.server as _hs

    creds_path = tmp_path / ".ensemble" / "stage.toml"

    import ensemble.stage as _stage_mod
    monkeypatch.setattr(_stage_mod, "_CREDS_PATH", creds_path)

    api_key_received = {}
    callback_done = threading.Event()

    class SimulatedCallbackHandler(_hs.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            keys = params.get("api_key", [])
            if keys:
                api_key_received["key"] = keys[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            callback_done.set()

        def log_message(self, fmt, *a):
            pass

    server = _hs.HTTPServer(("127.0.0.1", 0), SimulatedCallbackHandler)
    port = server.server_address[1]

    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()

    callback_url = f"http://127.0.0.1:{port}/?api_key=sim_test_key_xyz"
    import urllib.request
    with urllib.request.urlopen(callback_url) as resp:
        resp.read()

    callback_done.wait(timeout=5)
    assert api_key_received.get("key") == "sim_test_key_xyz"

    _stage_mod.write_credentials("sim_test_key_xyz", user_login="simuser")
    assert creds_path.exists()
    content = creds_path.read_text()
    assert "sim_test_key_xyz" in content
    mode = creds_path.stat().st_mode & 0o777
    assert mode == 0o600


# ---------------------------------------------------------------------------
# test_stage_whoami
# ---------------------------------------------------------------------------

def test_stage_whoami_prints_user(monkeypatch, tmp_path, capsys):
    """whoami prints github_login and default_org from a mock server."""
    from ensemble.stage import _CREDS_PATH

    responses = {
        "/v1/me": {
            "id": "usr_123",
            "github_login": "mockeduser",
            "email": "mocked@example.com",
            "default_org_slug": "mockedorg",
        }
    }
    server, base_url = _start_mock_server(responses)

    try:
        creds_path = tmp_path / ".ensemble" / "stage.toml"
        creds_path.parent.mkdir(parents=True, exist_ok=True)
        creds_path.write_text(
            f'[credentials]\napi_key = "mock_key"\nbase_url = "{base_url}"\n'
        )
        creds_path.chmod(0o600)

        import ensemble.stage as _stage_mod
        monkeypatch.setattr(_stage_mod, "_CREDS_PATH", creds_path)
        monkeypatch.delenv("ENSEMBLE_STAGE_API_KEY", raising=False)

        from ensemble.cli_stage import cmd_whoami

        class FakeArgs:
            pass

        cmd_whoami(FakeArgs())
        captured = capsys.readouterr()
        assert "mockeduser" in captured.out
        assert "mockedorg" in captured.out
    finally:
        server.shutdown()
