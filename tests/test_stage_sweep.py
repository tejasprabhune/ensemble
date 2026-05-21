"""Stage sweep coordination tests.

All three tests use a simple in-process mock HTTP server so no real
Stage server is needed.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
from pathlib import Path
from typing import List

import pytest


# ---------------------------------------------------------------------------
# Shared mock server helpers (same pattern as test_stage_sink.py)
# ---------------------------------------------------------------------------

class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            parsed = json.loads(body) if body else {}
        except ValueError:
            parsed = {}
        self.server._requests.append({"method": "POST", "path": self.path, "body": parsed})
        resp_body = json.dumps(self.server._responses.get(self.path, {"ok": True})).encode()
        self.send_response(self.server._status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    def log_message(self, fmt, *args):
        pass


def _start_server(responses: dict, status: int = 201) -> tuple:
    server = http.server.HTTPServer(("127.0.0.1", 0), _RecordingHandler)
    server._requests = []
    server._responses = responses
    server._status = status
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, f"http://127.0.0.1:{port}"


def _wait(server, n: int, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(server._requests) >= n:
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_sweep_creates_parent_on_stage(tmp_path: Path, monkeypatch):
    """cli_sweep.run() POSTs to /v1/projects/.../sweeps before running cells."""
    sweep_id = "sweep-abc-123"
    sweep_url = "http://mock/sweeps/sweep-abc-123"
    run_url = "http://mock/runs/run-xyz"

    responses = {
        "/v1/projects/myorg/myproj/sweeps": {"id": sweep_id, "url": sweep_url},
        "/v1/projects/myorg/myproj/runs": {"id": "run-xyz", "url": run_url},
        "/v1/runs/run-xyz/events": {"accepted": 0},
        "/v1/runs/run-xyz/status": {"ok": True},
        f"/v1/sweeps/{sweep_id}/status": {"ok": True},
    }
    server, base_url = _start_server(responses)

    try:
        monkeypatch.setenv("ENSEMBLE_STAGE_API_KEY", "sweep_test_key")
        monkeypatch.setenv("ENSEMBLE_STAGE_PROJECT", "myorg/myproj")
        monkeypatch.setenv("ENSEMBLE_STAGE_BASE_URL", base_url)
        monkeypatch.delenv("ENSEMBLE_STAGE_ENABLED", raising=False)

        _write_sweep_world(tmp_path)
        monkeypatch.chdir(tmp_path)

        from ensemble.cli_sweep import main
        rc = main(["run", str(tmp_path / "sweep.toml")])
        assert rc == 0

        _wait(server, 1)
        sweep_creates = [
            r for r in server._requests
            if "/sweeps" in r["path"] and "status" not in r["path"]
        ]
        assert len(sweep_creates) >= 1, f"No sweep create call. Requests: {server._requests}"
        assert sweep_creates[0]["body"].get("config") is not None
    finally:
        server.shutdown()


def test_sweep_child_runs_reference_parent(tmp_path: Path, monkeypatch):
    """Child runs' create-run bodies include sweep_id when Stage is configured."""
    sweep_id = "sweep-parent-99"

    responses = {
        "/v1/projects/myorg/myproj/sweeps": {"id": sweep_id, "url": "http://mock/sweeps/parent"},
        "/v1/projects/myorg/myproj/runs": {"id": "child-run", "url": "http://mock/runs/child"},
        "/v1/runs/child-run/events": {"accepted": 0},
        "/v1/runs/child-run/status": {"ok": True},
        f"/v1/sweeps/{sweep_id}/status": {"ok": True},
    }
    server, base_url = _start_server(responses)

    try:
        monkeypatch.setenv("ENSEMBLE_STAGE_API_KEY", "sweep_child_key")
        monkeypatch.setenv("ENSEMBLE_STAGE_PROJECT", "myorg/myproj")
        monkeypatch.setenv("ENSEMBLE_STAGE_BASE_URL", base_url)
        monkeypatch.delenv("ENSEMBLE_STAGE_ENABLED", raising=False)

        _write_sweep_world(tmp_path, scenario_suffix="child")
        monkeypatch.chdir(tmp_path)

        from ensemble.cli_sweep import main
        rc = main(["run", str(tmp_path / "sweep.toml")])
        assert rc == 0

        _wait(server, 2)
        run_creates = [
            r for r in server._requests
            if "/runs" in r["path"]
            and "events" not in r["path"]
            and "status" not in r["path"]
        ]
        assert len(run_creates) >= 1, f"No create-run call. Requests: {server._requests}"
        body = run_creates[0]["body"]
        assert body.get("sweep_id") == sweep_id, (
            f"Expected sweep_id={sweep_id!r} in create-run body, got: {body}"
        )
    finally:
        server.shutdown()


def test_sweep_status_updates_on_completion(tmp_path: Path, monkeypatch):
    """After all cells complete, cli_sweep posts status=completed to Stage."""
    sweep_id = "sweep-final-77"

    responses = {
        "/v1/projects/myorg/myproj/sweeps": {"id": sweep_id, "url": "http://mock/sweeps/final"},
        "/v1/projects/myorg/myproj/runs": {"id": "run-fin", "url": "http://mock/runs/fin"},
        "/v1/runs/run-fin/events": {"accepted": 0},
        "/v1/runs/run-fin/status": {"ok": True},
        f"/v1/sweeps/{sweep_id}/status": {"ok": True},
    }
    server, base_url = _start_server(responses)

    try:
        monkeypatch.setenv("ENSEMBLE_STAGE_API_KEY", "sweep_status_key")
        monkeypatch.setenv("ENSEMBLE_STAGE_PROJECT", "myorg/myproj")
        monkeypatch.setenv("ENSEMBLE_STAGE_BASE_URL", base_url)
        monkeypatch.delenv("ENSEMBLE_STAGE_ENABLED", raising=False)

        _write_sweep_world(tmp_path, scenario_suffix="fin")
        monkeypatch.chdir(tmp_path)

        from ensemble.cli_sweep import main
        rc = main(["run", str(tmp_path / "sweep.toml")])
        assert rc == 0

        _wait(server, 3)
        status_calls = [
            r for r in server._requests
            if f"/sweeps/{sweep_id}/status" in r["path"]
        ]
        assert len(status_calls) >= 1, f"No sweep status call. Requests: {server._requests}"
        assert status_calls[-1]["body"].get("status") == "completed"
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _write_sweep_world(tmp_path: Path, scenario_suffix: str = "sw") -> None:
    pkg = f"stgsweep_{scenario_suffix}"
    world = f"stgsweep_{scenario_suffix}"
    scenario = f"stgsweep_{scenario_suffix}.run"
    (tmp_path / "world.toml").write_text(
        f'[world]\nname = "{world}"\npython_package = "{pkg}"\n'
    )
    (tmp_path / f"{pkg}.py").write_text(
        f'from ensemble import register_world\nregister_world("{world}")\n'
    )
    (tmp_path / "scenarios").mkdir(exist_ok=True)
    fname = f"sweep_{scenario_suffix}"
    (tmp_path / "scenarios" / "__init__.py").write_text(f"from . import {fname}\n")
    (tmp_path / "scenarios" / f"{fname}.py").write_text(
        f"import {pkg}  # noqa: F401\n"
        "from ensemble import scenario\n"
        "\n"
        f'@scenario("{scenario}", world="{world}")\n'
        f"async def {fname}(world):\n"
        "    yield world.until(world.turn_count >= 1)\n"
        '    yield {"ok": 1.0}\n'
    )
    (tmp_path / "sweep.toml").write_text(
        f'[sweep]\nscenario = "{scenario}"\nworld = "{world}"\n'
        f'package_dir = "{tmp_path}"\n'
        f'traces_dir = "{tmp_path / "out"}"\n'
        "[sweep.flags]\nbackend = [\"mock\"]\n"
    )
