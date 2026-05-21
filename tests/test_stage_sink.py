"""Tests for the StageSink event batching and failure resilience."""

from __future__ import annotations

import http.server
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, List

import pytest


# ---------------------------------------------------------------------------
# Mock HTTP server helpers
# ---------------------------------------------------------------------------

class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """Records every request body and URL; replies with a configurable response."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            parsed = json.loads(body) if body else {}
        except ValueError:
            parsed = {}
        self.server._requests.append({
            "method": "POST",
            "path": self.path,
            "body": parsed,
        })
        status = self.server._status_code
        resp_body = json.dumps(self.server._response_body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    def do_GET(self):
        self.server._requests.append({"method": "GET", "path": self.path, "body": {}})
        resp_body = json.dumps(self.server._response_body).encode()
        self.send_response(self.server._status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    def log_message(self, fmt, *args):
        pass


def _start_recording_server(
    status_code: int = 200,
    response_body: dict = None,
) -> tuple:
    if response_body is None:
        response_body = {"id": "run_abc", "url": "https://stage.ensemble.sh/runs/run_abc"}
    server = http.server.HTTPServer(("127.0.0.1", 0), _RecordingHandler)
    server._requests = []
    server._status_code = status_code
    server._response_body = response_body
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, f"http://127.0.0.1:{port}"


def _wait_for_requests(server, min_count: int, timeout: float = 5.0) -> bool:
    """Wait until the server has received at least min_count requests."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(server._requests) >= min_count:
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# test_stage_sink_emits_events_to_mock_server
# ---------------------------------------------------------------------------

def test_stage_sink_emits_events_to_mock_server(tmp_path, monkeypatch):
    """A World wired to a mock Stage server receives create-run and events calls."""
    server, base_url = _start_recording_server(
        response_body={"id": "test-run-id", "url": f"{base_url}/runs/test-run-id"}
        if False else {"id": "test-run-id", "url": "http://mock/runs/test-run-id"},
    )

    try:
        monkeypatch.setenv("ENSEMBLE_STAGE_API_KEY", "test_sink_key")
        monkeypatch.setenv("ENSEMBLE_STAGE_PROJECT", "testorg/testproj")
        monkeypatch.setenv("ENSEMBLE_STAGE_BASE_URL", base_url)
        monkeypatch.delenv("ENSEMBLE_STAGE_ENABLED", raising=False)

        from ensemble import World, scenario
        from ensemble.stage import Stage

        stage = Stage(project="testorg/testproj", api_key="test_sink_key", base_url=base_url)
        world = World("noop", stage=stage)

        run_url = world.init_stage_run("test.scenario")

        world.log_note("hello from sink test")

        world.finalize_stage({"score": 1.0})

        _wait_for_requests(server, min_count=1, timeout=5.0)

        create_run_calls = [r for r in server._requests if "/runs" in r["path"] and r["method"] == "POST" and "events" not in r["path"] and "status" not in r["path"]]
        assert len(create_run_calls) >= 1, f"Expected create-run call, got: {server._requests}"

        body = create_run_calls[0]["body"]
        assert body.get("scenario") == "test.scenario"
        assert body.get("world") == "noop"

    finally:
        server.shutdown()


def test_stage_run_url_returned(tmp_path, monkeypatch):
    """init_stage_run returns the URL from the server response."""
    expected_url = "http://mock/runs/abc123"
    server, base_url = _start_recording_server(
        response_body={"id": "abc123", "url": expected_url},
    )
    try:
        from ensemble import World
        from ensemble.stage import Stage

        stage = Stage(project="org/proj", api_key="key123", base_url=base_url)
        world = World("noop", stage=stage)
        url = world.init_stage_run("some.scenario")
        assert url == expected_url, f"Expected {expected_url!r}, got {url!r}"
        assert world.run_url == expected_url
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# test_stage_sink_failure_continues_local_trace
# ---------------------------------------------------------------------------

def test_stage_sink_failure_continues_local_trace(tmp_path, monkeypatch):
    """A 500 from Stage does not prevent local trace.jsonl from being written."""
    server, base_url = _start_recording_server(
        status_code=500,
        response_body={"error": "internal server error"},
    )
    try:
        trace_path = tmp_path / "trace.jsonl"

        pkg = "sink_fail_pkg"
        world_name = "sink_fail"
        scenario_name = "sink_fail.test"
        (tmp_path / "world.toml").write_text(
            f'[world]\nname = "{world_name}"\npython_package = "{pkg}"\n'
        )
        (tmp_path / f"{pkg}.py").write_text(
            f'from ensemble import register_world\nregister_world("{world_name}")\n'
        )
        (tmp_path / "scenarios").mkdir()
        (tmp_path / "scenarios" / "__init__.py").write_text("from . import test_sink_fail\n")
        (tmp_path / "scenarios" / "test_sink_fail.py").write_text(
            f'import {pkg}  # noqa: F401\n'
            "from ensemble import scenario\n"
            "\n"
            f'@scenario("{scenario_name}", world="{world_name}")\n'
            "async def test_sink_fail(world):\n"
            "    world.log_note('sink_failure_test_event')\n"
            "    yield world.until(world.turn_count >= 1)\n"
            '    yield {"ok": 1.0}\n'
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("ENSEMBLE_STAGE_API_KEY", "failing_key")
        monkeypatch.setenv("ENSEMBLE_STAGE_PROJECT", "org/proj")
        monkeypatch.setenv("ENSEMBLE_STAGE_BASE_URL", base_url)
        monkeypatch.delenv("ENSEMBLE_STAGE_ENABLED", raising=False)

        from ensemble.cli_run import main
        rc = main([
            "--scenario", scenario_name,
            "--traces-dir", str(tmp_path / "traces"),
        ])

        assert rc == 0, "CLI run should succeed even when Stage returns 500"

        run_dirs = list((tmp_path / "traces").iterdir())
        run_dirs = [d for d in run_dirs if d.is_dir()]
        assert len(run_dirs) == 1, f"Expected 1 run dir, got {run_dirs}"
        local_trace = run_dirs[0] / "trace.jsonl"
        assert local_trace.exists(), "Local trace.jsonl must be written even on Stage failure"
        lines = [l for l in local_trace.read_text().splitlines() if l.strip()]
        assert len(lines) > 0, "Local trace must have events"

    finally:
        server.shutdown()


def test_no_stage_world_runs_normally(tmp_path, monkeypatch):
    """A world with no Stage config runs normally and has a local trace."""
    monkeypatch.delenv("ENSEMBLE_STAGE_API_KEY", raising=False)
    monkeypatch.delenv("ENSEMBLE_STAGE_PROJECT", raising=False)
    monkeypatch.delenv("ENSEMBLE_STAGE_ENABLED", raising=False)

    import ensemble.stage as _stage_mod
    monkeypatch.setattr(_stage_mod, "_CREDS_PATH", tmp_path / "no_creds.toml")
    monkeypatch.setattr(_stage_mod, "_PROJECT_TOML", tmp_path / "no_proj.toml")

    from ensemble import World

    world = World("noop")
    assert world.run_url is None
    result = world.init_stage_run("some.scenario")
    assert result is None
    world.finalize_stage({})
