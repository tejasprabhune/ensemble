"""Phase 6: merged runs list, show, and compare with Stage."""

from __future__ import annotations

import http.server
import json
import threading
import time
from pathlib import Path
from typing import Dict, List

import pytest


# ---------------------------------------------------------------------------
# Mock Stage server
# ---------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        resp = self.server._responses.get(self.path)
        if resp is None:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def _start(responses: dict) -> tuple:
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    server._responses = responses
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_local_index(traces_dir: Path, runs: List[Dict]) -> None:
    traces_dir.mkdir(parents=True, exist_ok=True)
    index = traces_dir / "runs.jsonl"
    with index.open("w") as f:
        for r in runs:
            f.write(json.dumps(r) + "\n")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_runs_list_merges_local_and_stage(tmp_path, monkeypatch):
    """cmd_list shows rows from both local index and Stage."""
    local_id = "019542a3-0000-7000-aaaa-000000000001"
    stage_only_id = "019542a3-0000-7000-bbbb-000000000002"

    traces_dir = tmp_path / "traces"
    _make_local_index(traces_dir, [{
        "run_id": local_id,
        "scenario": "plank.smoke",
        "world": "plank",
        "backend": "mock",
        "started_at": 1700000000.0,
        "finished_at": 1700000010.0,
        "duration_s": 10.0,
        "scores": {"ok": 1.0},
        "costs": {},
        "trace_path": str(traces_dir / local_id / "trace.jsonl"),
    }])

    responses = {
        "/v1/projects/myorg/myproj/runs?limit=50&sort=created_at:desc": {
            "runs": [
                {
                    "id": stage_only_id,
                    "scenario": "plank.smoke",
                    "world": "plank",
                    "backend": "anthropic",
                    "status": "completed",
                    "started_at": "2023-11-15T00:00:30Z",
                    "ended_at": "2023-11-15T00:00:40Z",
                    "wall_time_ms": 10000,
                    "url": f"http://stage/{stage_only_id}",
                }
            ],
            "next_cursor": None,
        }
    }
    server, base_url = _start(responses)
    try:
        monkeypatch.setenv("ENSEMBLE_STAGE_API_KEY", "test_key")
        monkeypatch.setenv("ENSEMBLE_STAGE_PROJECT", "myorg/myproj")
        monkeypatch.setenv("ENSEMBLE_STAGE_BASE_URL", base_url)
        monkeypatch.delenv("ENSEMBLE_STAGE_ENABLED", raising=False)

        from ensemble.cli_runs import cmd_list
        import argparse, io, sys
        args = argparse.Namespace(
            traces_dir=traces_dir, scenario=None, limit=50
        )
        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            rc = cmd_list(args)
        finally:
            sys.stdout = old_stdout
        out = buf.getvalue()

        assert rc == 0
        assert local_id[:20] in out, f"local run not in output:\n{out}"
        assert stage_only_id[:20] in out, f"stage run not in output:\n{out}"
        assert "location" in out.lower() or "local" in out or "stage" in out
    finally:
        server.shutdown()


def test_same_id_collapses_to_single_row(tmp_path, monkeypatch):
    """A run that exists both locally and on Stage appears as one row."""
    shared_id = "019542a3-0000-7000-cccc-000000000003"

    traces_dir = tmp_path / "traces"
    _make_local_index(traces_dir, [{
        "run_id": shared_id,
        "scenario": "plank.smoke",
        "world": "plank",
        "backend": "mock",
        "started_at": 1700000000.0,
        "finished_at": 1700000010.0,
        "duration_s": 10.0,
        "scores": {"ok": 1.0},
        "costs": {},
        "trace_path": str(traces_dir / shared_id / "trace.jsonl"),
    }])

    responses = {
        "/v1/projects/myorg/myproj/runs?limit=50&sort=created_at:desc": {
            "runs": [{
                "id": shared_id,
                "scenario": "plank.smoke",
                "world": "plank",
                "backend": "mock",
                "status": "completed",
                "started_at": "2023-11-15T00:00:00Z",
                "ended_at": "2023-11-15T00:00:10Z",
                "wall_time_ms": 10000,
                "url": f"http://stage/{shared_id}",
            }],
            "next_cursor": None,
        }
    }
    server, base_url = _start(responses)
    try:
        monkeypatch.setenv("ENSEMBLE_STAGE_API_KEY", "test_key")
        monkeypatch.setenv("ENSEMBLE_STAGE_PROJECT", "myorg/myproj")
        monkeypatch.setenv("ENSEMBLE_STAGE_BASE_URL", base_url)
        monkeypatch.delenv("ENSEMBLE_STAGE_ENABLED", raising=False)

        from ensemble.cli_runs import _merge_runs, _fetch_stage_runs
        import argparse, io, sys

        from ensemble.cli_runs import _load_index
        local = _load_index(traces_dir)
        stage = _fetch_stage_runs(limit=50)
        merged = _merge_runs(local, stage)

        ids = [r["run_id"] for r in merged]
        assert ids.count(shared_id) == 1, f"Expected exactly 1 row for {shared_id}, got {ids}"
        row = next(r for r in merged if r["run_id"] == shared_id)
        assert row.get("location") == "local+stage"
    finally:
        server.shutdown()


def test_compare_handles_mixed_sources(tmp_path, monkeypatch):
    """cmd_compare resolves one run locally, one from Stage."""
    local_id = "019542a3-0000-7000-dddd-000000000004"
    stage_id = "019542a3-0000-7000-eeee-000000000005"

    traces_dir = tmp_path / "traces"
    _make_local_index(traces_dir, [{
        "run_id": local_id,
        "scenario": "plank.smoke",
        "world": "plank",
        "backend": "mock",
        "started_at": 1700000000.0,
        "finished_at": 1700000010.0,
        "duration_s": 10.0,
        "scores": {"ok": 0.8},
        "costs": {},
        "trace_path": str(traces_dir / local_id / "trace.jsonl"),
    }])

    responses = {
        f"/v1/runs/{stage_id}": {
            "id": stage_id,
            "scenario": "plank.smoke",
            "world": "plank",
            "backend": "anthropic",
            "status": "completed",
            "started_at": "2023-11-15T00:00:00Z",
            "ended_at": "2023-11-15T00:00:15Z",
            "wall_time_ms": 15000,
            "outcome": {"scores": {"ok": 0.9}},
            "url": f"http://stage/{stage_id}",
        },
    }
    server, base_url = _start(responses)
    try:
        monkeypatch.setenv("ENSEMBLE_STAGE_API_KEY", "test_key")
        monkeypatch.setenv("ENSEMBLE_STAGE_PROJECT", "myorg/myproj")
        monkeypatch.setenv("ENSEMBLE_STAGE_BASE_URL", base_url)
        monkeypatch.delenv("ENSEMBLE_STAGE_ENABLED", raising=False)

        from ensemble.cli_runs import cmd_compare
        import argparse, io, sys

        args = argparse.Namespace(
            traces_dir=traces_dir,
            a=local_id,
            b=stage_id,
        )
        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            rc = cmd_compare(args)
        finally:
            sys.stdout = old_stdout
        out = buf.getvalue()

        assert rc == 0
        assert "ok" in out
        assert "0.8" in out
        assert "0.9" in out
    finally:
        server.shutdown()
