"""Stage integration for the training pipeline.

Emits training run lifecycle events and step metrics to the Stage server.
HTTP calls use Python's built-in urllib so no extra packages are needed
beyond the base training dependencies.

Metric flushing happens on a background daemon thread so individual
training steps are not blocked by network I/O. Metrics accumulate in
a queue and flush every 30 seconds or when 50 metric points have built
up (training is slower than inference, so the timer is longer than the
run-event sink).
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


PROD_BASE_URL = "https://ensemble-stage.fly.dev"


@dataclass
class StageTrainingConfig:
    api_key: str
    project: str
    base_url: str = PROD_BASE_URL

    @property
    def org_slug(self) -> str:
        return self.project.split("/", 1)[0]

    @property
    def project_slug(self) -> str:
        parts = self.project.split("/", 1)
        return parts[1] if len(parts) == 2 else self.project

    @classmethod
    def from_env(cls) -> Optional["StageTrainingConfig"]:
        if os.environ.get("ENSEMBLE_STAGE_ENABLED", "").strip() == "0":
            return None
        api_key = os.environ.get("ENSEMBLE_STAGE_API_KEY", "").strip()
        project = os.environ.get("ENSEMBLE_STAGE_PROJECT", "").strip()
        if not api_key or not project:
            return None
        base_url = os.environ.get("ENSEMBLE_STAGE_BASE_URL", PROD_BASE_URL).strip()
        return cls(api_key=api_key, project=project, base_url=base_url)


def _api_call(
    config: StageTrainingConfig,
    method: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    url = f"{config.base_url.rstrip('/')}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {config.api_key}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"Stage {method} {path} returned {e.code}: {detail}") from e


class StageTrainingReporter:
    """Posts training run metadata and step metrics to Stage.

    Obtain an instance via StageTrainingReporter.start(); call
    emit_metrics() from each training step and finish() at the end.
    """

    _FLUSH_INTERVAL_S = 30.0
    _FLUSH_BATCH = 50

    def __init__(
        self,
        config: StageTrainingConfig,
        training_run_id: str,
        url: str,
    ) -> None:
        self._config = config
        self._run_id = training_run_id
        self._url = url
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._thread.start()

    @classmethod
    def start(
        cls,
        config: StageTrainingConfig,
        persona_name: str,
        base_model: str,
        hyperparameters: Dict[str, Any],
    ) -> "StageTrainingReporter":
        path = f"/v1/projects/{config.org_slug}/{config.project_slug}/training_runs"
        resp = _api_call(config, "POST", path, {
            "persona_name": persona_name,
            "base_model": base_model,
            "hyperparameters": hyperparameters,
        })
        return cls(config, resp["id"], resp["url"])

    @property
    def run_url(self) -> str:
        return self._url

    @property
    def run_id(self) -> str:
        return self._run_id

    def emit_metrics(self, step: int, metrics: Dict[str, float]) -> None:
        """Buffer step metrics for the next flush. Non-blocking."""
        entries = [
            {"step": step, "metric_name": k, "value": float(v)}
            for k, v in metrics.items()
            if isinstance(v, (int, float))
        ]
        if entries:
            self._queue.put(entries)

    def finish(
        self,
        final_metrics: Optional[Dict[str, float]] = None,
        artifact_uri: str = "",
    ) -> None:
        """Flush remaining metrics, then POST the terminal status."""
        self._stop.set()
        self._thread.join(timeout=30)

        remaining: List[dict] = []
        while True:
            try:
                remaining.extend(self._queue.get_nowait())
            except queue.Empty:
                break
        if remaining:
            self._push_metrics(remaining)

        body: Dict[str, Any] = {"status": "completed"}
        if final_metrics:
            body["final_metrics"] = {k: float(v) for k, v in final_metrics.items()}
        if artifact_uri:
            body["artifact_uri"] = artifact_uri
        try:
            _api_call(self._config, "POST", f"/v1/training_runs/{self._run_id}/status", body)
        except Exception as e:
            print(f"warning: Stage training run status update failed: {e}")

    def _flush_loop(self) -> None:
        pending: List[dict] = []
        last_flush = time.monotonic()
        while not self._stop.is_set():
            try:
                entries = self._queue.get(timeout=1.0)
                pending.extend(entries)
            except queue.Empty:
                pass
            now = time.monotonic()
            if pending and (
                len(pending) >= self._FLUSH_BATCH
                or now - last_flush >= self._FLUSH_INTERVAL_S
            ):
                self._push_metrics(pending)
                pending = []
                last_flush = now

    def _push_metrics(self, entries: List[dict]) -> None:
        try:
            _api_call(
                self._config,
                "POST",
                f"/v1/training_runs/{self._run_id}/metrics",
                {"metrics": entries},
            )
        except Exception as e:
            print(f"warning: Stage metric flush failed: {e}")
