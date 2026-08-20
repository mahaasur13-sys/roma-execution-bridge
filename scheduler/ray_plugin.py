"""
ROMA Ray Integration Plugin
Executes distributed jobs on a Ray cluster via REST API (Ray Dashboard).
No ray library required — pure HTTP to Ray Job Submission API.
"""

import logging
import os
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
import json

logger = logging.getLogger("roma.ray")

RAY_ENABLED = os.environ.get("RAY_ENABLED", "false").lower() == "true"
RAY_ADDRESS = os.environ.get("RAY_ADDRESS", "http://127.0.0.1:8265")
RAY_HEAD_NODE = os.environ.get("RAY_HEAD_NODE", "")

# Map Ray job statuses to ROMA statuses
RAY_STATUS_MAP = {
    "PENDING": "pending",
    "RUNNING": "running",
    "SUCCEEDED": "completed",
    "FAILED": "failed",
    "STOPPED": "cancelled",
}


class RayPlugin:
    """Execute jobs on a Ray cluster via Dashboard REST API."""

    def __init__(self):
        self.enabled = RAY_ENABLED
        self.address = RAY_ADDRESS.rstrip("/")
        self.head_node = RAY_HEAD_NODE
        self._session_ok = False

    def _check_health(self) -> bool:
        """Quick health check against Ray Dashboard."""
        try:
            resp = self._api_get("/api/version")
            self._session_ok = resp.get("ray_version") is not None
            return self._session_ok
        except Exception:
            self._session_ok = False
            return False

    def _api_get(self, path: str, timeout: int = 10) -> dict:
        """GET request to Ray Dashboard API."""
        url = f"{self.address}{path}"
        req = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            raise RuntimeError(f"Ray API error {e.code}: {e.reason}")
        except URLError as e:
            raise RuntimeError(f"Ray unavailable: {e.reason}")

    def _api_post(self, path: str, data: dict, timeout: int = 30) -> dict:
        """POST request to Ray Dashboard API."""
        url = f"{self.address}{path}"
        body = json.dumps(data).encode()
        req = Request(url, data=body, headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            err_body = e.read().decode() if e.fp else str(e)
            raise RuntimeError(f"Ray API error {e.code}: {err_body[:200]}")
        except URLError as e:
            raise RuntimeError(f"Ray unavailable: {e.reason}")

    def execute(self, job: dict) -> dict:
        """Submit job to Ray via Job Submission API."""
        if not self.enabled:
            return {"ray_job_id": None, "status": "local_emulation", "message": "Ray disabled — running locally"}

        try:
            entrypoint = job.get("entrypoint", job.get("script", "echo 'Ray job'"))
            runtime_env = job.get("runtime_env", {})
            if job.get("gpu_count", 0) > 0:
                runtime_env["gpu"] = job["gpu_count"]

            # Job Submission API: POST /api/jobs/
            payload = {
                "entrypoint": entrypoint,
                "submission_id": f"roma-{job.get('job_id', 'unknown')[:12]}",
            }
            if runtime_env:
                payload["runtime_env"] = runtime_env

            result = self._api_post("/api/jobs/", payload)
            ray_job_id = result.get("submission_id") or result.get("job_id")

            logger.info("Ray job submitted: %s → %s", job.get("job_id"), ray_job_id)
            return {"ray_job_id": ray_job_id, "status": "submitted", "message": "Job accepted"}

        except RuntimeError:
            raise
        except Exception as e:
            logger.error("Ray execute error: %s", e)
            return {"ray_job_id": None, "status": "failure", "message": str(e)}

    def get_status(self, ray_job_id: str) -> dict:
        """Query job status from Ray Dashboard."""
        if not self.enabled:
            return {"ray_job_id": ray_job_id, "status": "unknown", "message": "Ray disabled"}

        try:
            result = self._api_get(f"/api/jobs/{ray_job_id}")
            raw_status = result.get("status", "UNKNOWN")
            mapped = RAY_STATUS_MAP.get(raw_status, raw_status.lower())
            return {
                "ray_job_id": ray_job_id,
                "status": mapped,
                "ray_status": raw_status,
                "message": result.get("message", ""),
            }
        except Exception as e:
            logger.error("Ray status error for %s: %s", ray_job_id, e)
            return {"ray_job_id": ray_job_id, "status": "error", "message": str(e)}

    def cancel(self, ray_job_id: str) -> dict:
        """Cancel a Ray job via Dashboard API."""
        if not self.enabled:
            return {"ray_job_id": ray_job_id, "status": "not_cancelled", "message": "Ray disabled"}

        try:
            # POST /api/jobs/{job_id}/stop
            result = self._api_post(f"/api/jobs/{ray_job_id}/stop", {})
            return {"ray_job_id": ray_job_id, "status": "cancelled", "message": str(result)}
        except Exception as e:
            logger.error("Ray cancel error for %s: %s", ray_job_id, e)
            return {"ray_job_id": ray_job_id, "status": "error", "message": str(e)}


ray_plugin = RayPlugin()
