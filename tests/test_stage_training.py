"""Stage training run coordination tests.

Tests exercise StageTrainingReporter directly against a mock HTTP
server so torch/transformers are not required.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Mock HTTP server
# ---------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            parsed = json.loads(body) if body else {}
        except ValueError:
            parsed = {}
        self.server._requests.append({"path": self.path, "body": parsed})
        resp = json.dumps(self.server._responses.get(self.path, {"ok": True})).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, fmt, *args):
        pass


def _start(responses: dict) -> tuple:
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    server._requests = []
    server._responses = responses
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


def _wait(server, n: int, timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(server._requests) >= n:
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_training_run_created_on_start():
    """StageTrainingReporter.start() POSTs to /v1/projects/.../training_runs."""
    training_id = "tr-abc-123"
    training_url = "http://mock/training_runs/tr-abc-123"
    responses = {
        "/v1/projects/myorg/myproj/training_runs": {
            "id": training_id,
            "url": training_url,
        },
        f"/v1/training_runs/{training_id}/status": {"ok": True},
    }
    server, base_url = _start(responses)
    try:
        from ensemble_train.stage_reporter import StageTrainingConfig, StageTrainingReporter

        cfg = StageTrainingConfig(api_key="test_key", project="myorg/myproj", base_url=base_url)
        reporter = StageTrainingReporter.start(
            cfg,
            persona_name="popcorn-v2",
            base_model="claude-haiku-4-5",
            hyperparameters={"learning_rate": 1e-4, "batch_size": 32},
        )

        assert reporter.run_id == training_id
        assert reporter.run_url == training_url

        _wait(server, 1)
        creates = [r for r in server._requests if "/training_runs" in r["path"] and "metrics" not in r["path"] and "status" not in r["path"]]
        assert len(creates) >= 1, f"No create call. Got: {server._requests}"
        body = creates[0]["body"]
        assert body["persona_name"] == "popcorn-v2"
        assert body["base_model"] == "claude-haiku-4-5"
        assert "learning_rate" in body["hyperparameters"]

        reporter.finish()
    finally:
        server.shutdown()


def test_metrics_streamed_during_training():
    """emit_metrics() sends step metrics to /v1/training_runs/{id}/metrics."""
    training_id = "tr-metrics-456"
    responses = {
        "/v1/projects/myorg/myproj/training_runs": {
            "id": training_id,
            "url": "http://mock/training_runs/tr-metrics-456",
        },
        f"/v1/training_runs/{training_id}/metrics": {"accepted": 2},
        f"/v1/training_runs/{training_id}/status": {"ok": True},
    }
    server, base_url = _start(responses)
    try:
        from ensemble_train.stage_reporter import StageTrainingConfig, StageTrainingReporter

        cfg = StageTrainingConfig(api_key="test_key", project="myorg/myproj", base_url=base_url)
        reporter = StageTrainingReporter.start(
            cfg, persona_name="p", base_model="m", hyperparameters={},
        )
        reporter.emit_metrics(10, {"train_loss": 1.42, "eval_loss": 1.57})
        reporter.emit_metrics(20, {"train_loss": 1.18, "eval_loss": 1.31})

        reporter.finish()

        _wait(server, 2)
        metric_calls = [r for r in server._requests if "/metrics" in r["path"]]
        assert len(metric_calls) >= 1, f"No metrics call. Got: {server._requests}"

        all_entries = []
        for call in metric_calls:
            all_entries.extend(call["body"].get("metrics", []))

        metric_names = {e["metric_name"] for e in all_entries}
        assert "train_loss" in metric_names
        steps = {e["step"] for e in all_entries}
        assert 10 in steps or 20 in steps
    finally:
        server.shutdown()


def test_artifact_registered_on_completion():
    """finish() includes artifact_uri and final_metrics in the status POST."""
    training_id = "tr-done-789"
    responses = {
        "/v1/projects/myorg/myproj/training_runs": {
            "id": training_id,
            "url": "http://mock/training_runs/tr-done-789",
        },
        f"/v1/training_runs/{training_id}/status": {"ok": True},
    }
    server, base_url = _start(responses)
    try:
        from ensemble_train.stage_reporter import StageTrainingConfig, StageTrainingReporter

        cfg = StageTrainingConfig(api_key="test_key", project="myorg/myproj", base_url=base_url)
        reporter = StageTrainingReporter.start(
            cfg, persona_name="p", base_model="m", hyperparameters={},
        )
        reporter.finish(
            final_metrics={"train_loss": 0.32, "eval_loss": 0.41},
            artifact_uri="gs://my-bucket/adapters/popcorn-v2.safetensors",
        )

        _wait(server, 2)
        status_calls = [r for r in server._requests if "/status" in r["path"]]
        assert len(status_calls) >= 1, f"No status call. Got: {server._requests}"
        body = status_calls[-1]["body"]
        assert body["status"] == "completed"
        assert body.get("artifact_uri") == "gs://my-bucket/adapters/popcorn-v2.safetensors"
        assert body.get("final_metrics", {}).get("train_loss") == pytest.approx(0.32)
    finally:
        server.shutdown()
