"""Phase 7 (trace viewer module extraction) and Phase 8 (bulk push) tests."""

from __future__ import annotations

import http.server
import json
import threading
import time
from pathlib import Path
from typing import Dict, List

import pytest


# ---------------------------------------------------------------------------
# Phase 7: trace viewer module structure
# ---------------------------------------------------------------------------

SHARED_DIR = Path(__file__).parent.parent / "shared" / "trace-viewer"


def test_local_jsonl_source_file_exists():
    """shared/trace-viewer/sources/local-jsonl.js exists and exports LocalJsonlSource."""
    src = SHARED_DIR / "sources" / "local-jsonl.js"
    assert src.exists(), f"missing: {src}"
    content = src.read_text()
    assert "LocalJsonlSource" in content
    assert "export class LocalJsonlSource" in content
    assert "onUpdate" in content
    assert "start" in content
    assert "isComplete" in content


def test_stage_polling_source_file_exists():
    """shared/trace-viewer/sources/stage-polling.js exists and exports StagePollingSource."""
    src = SHARED_DIR / "sources" / "stage-polling.js"
    assert src.exists(), f"missing: {src}"
    content = src.read_text()
    assert "StagePollingSource" in content
    assert "export class StagePollingSource" in content
    assert "onUpdate" in content
    assert "start" in content
    assert "isComplete" in content


def test_viewer_module_exports_mountViewer():
    """shared/trace-viewer/viewer.js exports mountViewer and documents the DataSource contract."""
    src = SHARED_DIR / "viewer.js"
    assert src.exists(), f"missing: {src}"
    content = src.read_text()
    assert "export function mountViewer" in content
    assert "DataSource" in content
    assert "onUpdate" in content


def test_site_viewer_js_uses_shared_module():
    """site/viewer.js imports from the shared module."""
    site_viewer = Path(__file__).parent.parent / "site" / "viewer.js"
    assert site_viewer.exists()
    content = site_viewer.read_text()
    assert "shared/trace-viewer" in content
    assert "LocalJsonlSource" in content


# ---------------------------------------------------------------------------
# Phase 8: bulk push
# ---------------------------------------------------------------------------

class _PushHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server._get_requests.append(self.path)
        if "/runs/" in self.path and "events" not in self.path and "status" not in self.path:
            # Run lookup - return 404 to indicate not present
            if self.path in (self.server._existing_runs or []):
                resp = json.dumps({"id": "exists"}).encode()
                self.send_response(200)
            else:
                self.send_response(404)
                resp = b'{"error":{"code":"not_found","message":"not found"}}'
        else:
            self.send_response(200)
            resp = json.dumps({"ok": True}).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            parsed = json.loads(body) if body else {}
        except ValueError:
            parsed = {}
        self.server._post_requests.append({"path": self.path, "body": parsed})
        if "runs" in self.path and "events" not in self.path and "status" not in self.path:
            resp = json.dumps({"id": "new-run-id", "url": "http://mock/runs/new"}).encode()
            self.send_response(201)
        else:
            resp = json.dumps({"accepted": len(parsed.get("events", [])), "ok": True}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, fmt, *args):
        pass


def _start_push_server(existing_runs=None):
    server = http.server.HTTPServer(("127.0.0.1", 0), _PushHandler)
    server._get_requests = []
    server._post_requests = []
    server._existing_runs = existing_runs or []
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


def _write_run_dir(traces_dir: Path, run_id: str, events: List[Dict]) -> Path:
    run_dir = traces_dir / run_id
    run_dir.mkdir(parents=True)
    meta = {
        "run_id": run_id,
        "scenario": "test.smoke",
        "world": "test",
        "backend": "mock",
        "started_at": 1700000000.0,
        "finished_at": 1700000010.0,
        "scores": {"ok": 1.0},
    }
    (run_dir / "meta.json").write_text(json.dumps(meta))
    lines = "\n".join(json.dumps(e) for e in events)
    (run_dir / "trace.jsonl").write_text(lines + "\n")
    return run_dir


def test_push_creates_run_and_streams_events(tmp_path, monkeypatch):
    """stage push sends create-run and event batch POSTs for a local trace."""
    server, base_url = _start_push_server()
    try:
        run_id = "019542a3-0000-7000-ffff-000000000010"
        traces_dir = tmp_path / "traces"
        events = [
            {"tick": 1, "ts_ms": 1700000000000, "actor": None, "message_id": None,
             "payload": {"kind": "system", "note": "run started"}},
            {"tick": 2, "ts_ms": 1700000001000, "actor": "actor-abc", "message_id": None,
             "payload": {"kind": "agent_message", "text": "Hello"}},
        ]
        _write_run_dir(traces_dir, run_id, events)

        monkeypatch.setenv("ENSEMBLE_STAGE_API_KEY", "push_test_key")
        monkeypatch.setenv("ENSEMBLE_STAGE_PROJECT", "myorg/myproj")
        monkeypatch.setenv("ENSEMBLE_STAGE_BASE_URL", base_url)
        monkeypatch.delenv("ENSEMBLE_STAGE_ENABLED", raising=False)

        from ensemble.cli_stage import cmd_push
        import argparse

        args = argparse.Namespace(path=str(traces_dir))
        rc = cmd_push(args)
        assert rc == 0

        create_calls = [r for r in server._post_requests if "events" not in r["path"] and "status" not in r["path"] and "/runs" in r["path"]]
        assert len(create_calls) >= 1, f"No create-run call. Posts: {server._post_requests}"
        assert create_calls[0]["body"].get("id") == run_id

        event_calls = [r for r in server._post_requests if "events" in r["path"]]
        assert len(event_calls) >= 1, f"No events call. Posts: {server._post_requests}"
        batch = event_calls[0]["body"].get("events", [])
        assert len(batch) == 2
    finally:
        server.shutdown()


def test_push_skips_already_pushed(tmp_path, monkeypatch):
    """stage push skips runs that already exist on Stage."""
    run_id = "019542a3-0000-7000-ffff-000000000011"
    # Server returns 200 for this run's GET, signaling it exists.
    server, base_url = _start_push_server(existing_runs=[f"/v1/runs/{run_id}"])
    try:
        traces_dir = tmp_path / "traces"
        _write_run_dir(traces_dir, run_id, [
            {"tick": 1, "ts_ms": 1700000000000, "actor": None, "message_id": None,
             "payload": {"kind": "system", "note": "started"}},
        ])

        monkeypatch.setenv("ENSEMBLE_STAGE_API_KEY", "skip_test_key")
        monkeypatch.setenv("ENSEMBLE_STAGE_PROJECT", "myorg/myproj")
        monkeypatch.setenv("ENSEMBLE_STAGE_BASE_URL", base_url)
        monkeypatch.delenv("ENSEMBLE_STAGE_ENABLED", raising=False)

        from ensemble.cli_stage import cmd_push
        import argparse

        args = argparse.Namespace(path=str(traces_dir))
        rc = cmd_push(args)
        assert rc == 0

        create_calls = [r for r in server._post_requests if "events" not in r["path"] and "status" not in r["path"] and "/runs" in r["path"]]
        assert len(create_calls) == 0, f"Should have skipped, but got create calls: {create_calls}"
    finally:
        server.shutdown()


def test_push_glob_matches(tmp_path, monkeypatch):
    """stage push finds trace dirs under a parent directory."""
    server, base_url = _start_push_server()
    try:
        traces_dir = tmp_path / "traces"
        run_ids = [
            "019542a3-0000-7000-ffff-000000000020",
            "019542a3-0000-7000-ffff-000000000021",
        ]
        ev = [{"tick": 1, "ts_ms": 1700000000000, "actor": None, "message_id": None,
               "payload": {"kind": "system", "note": "started"}}]
        for rid in run_ids:
            _write_run_dir(traces_dir, rid, ev)

        monkeypatch.setenv("ENSEMBLE_STAGE_API_KEY", "glob_test_key")
        monkeypatch.setenv("ENSEMBLE_STAGE_PROJECT", "myorg/myproj")
        monkeypatch.setenv("ENSEMBLE_STAGE_BASE_URL", base_url)
        monkeypatch.delenv("ENSEMBLE_STAGE_ENABLED", raising=False)

        from ensemble.cli_stage import cmd_push
        import argparse

        args = argparse.Namespace(path=str(traces_dir))
        rc = cmd_push(args)
        assert rc == 0

        create_calls = [r for r in server._post_requests if "events" not in r["path"] and "status" not in r["path"] and "/runs" in r["path"]]
        assert len(create_calls) == 2, f"Expected 2 create calls, got: {len(create_calls)}"
    finally:
        server.shutdown()
