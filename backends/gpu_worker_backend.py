"""gpu_worker backend — routes jobs to a self-hosted ROMA GPU worker over HTTP.

The worker (gpu_worker/server.py) exposes /health, /execute, /status/{job_id}.
If the worker is down or reports gpu_available=false, dispatch returns an honest
status=failed with backend="gpu_worker" (no silent fallback to local).
"""
from __future__ import annotations

import logging
import os

import requests

from backends.base import BaseBackend, JobContext

logger = logging.getLogger("roma.backends.gpu_worker")

DEFAULT_WORKER_URL = "http://localhost:8000"
PROBE_TIMEOUT = 5  # short timeout for /health and /status probes


def _worker_url() -> str:
    return os.getenv("ROMA_GPU_WORKER_URL", DEFAULT_WORKER_URL).rstrip("/")


class GpuWorkerBackend(BaseBackend):
    """Routes execution to a self-hosted ROMA GPU worker."""

    backend_name = "gpu_worker"

    @property
    def enabled(self) -> bool:
        # Always selectable: availability is decided at dispatch time so we can
        # return an honest "failed" instead of silently falling back to local.
        return True

    @staticmethod
    def _worker_headers() -> dict:
        """Shared worker credential sent to gpu_worker /execute.

        If unset, no header is sent and the worker (which is fail-closed) will
        reject the request with 401.
        """
        token = os.getenv("ROMA_GPU_WORKER_TOKEN", "")
        return {"X-Worker-Token": token} if token else {}

    async def dispatch(self, ctx: JobContext) -> dict:
        url = _worker_url()
        try:
            resp = requests.get(f"{url}/health", timeout=PROBE_TIMEOUT)
        except Exception as exc:
            logger.warning("gpu_worker.dispatch_unreachable url=%s err=%s", url, exc)
            return {
                "backend": "gpu_worker",
                "status": "failed",
                "job_id": ctx.job_id,
                "message": f"gpu_worker unreachable at {url}: {exc}",
            }

        if resp.status_code != 200:
            return {
                "backend": "gpu_worker",
                "status": "failed",
                "job_id": ctx.job_id,
                "message": f"gpu_worker /health returned HTTP {resp.status_code}",
            }

        data = resp.json()
        if not data.get("gpu_available", False):
            return {
                "backend": "gpu_worker",
                "status": "failed",
                "job_id": ctx.job_id,
                "message": "gpu_worker has no GPU available (gpu_available=false)",
            }

        return {
            "backend": "gpu_worker",
            "status": "running",
            "job_id": ctx.job_id,
            "worker_id": data.get("worker_id"),
        }

    async def run_command(self, ctx: JobContext, command: str, timeout: int = 600) -> dict:
        url = _worker_url()
        payload = {"job_id": ctx.job_id, "command": command, "timeout": timeout}
        try:
            resp = requests.post(f"{url}/execute", json=payload, headers=self._worker_headers(), timeout=timeout + 30)
        except Exception as exc:
            return {"status": "failed", "output": "", "error": str(exc)}

        if resp.status_code != 200:
            detail = ""
            try:
                detail = resp.json().get("detail", "")
            except Exception:
                pass
            return {"status": "failed", "output": "", "error": detail or f"HTTP {resp.status_code}"}

        data = resp.json()
        worker_status = data.get("status", "failed")  # success | failed | timeout
        return {
            "status": "completed" if worker_status == "success" else "failed",
            "output": data.get("stdout", ""),
            "error": data.get("stderr", "") or None,
            "exit_code": data.get("returncode"),
        }

    async def get_status(self, job_id: str, instance_id: str | None = None) -> dict:
        url = _worker_url()
        try:
            resp = requests.get(f"{url}/health", timeout=PROBE_TIMEOUT)
        except Exception as exc:
            return {"status": "failed", "job_id": job_id, "message": str(exc)}
        if resp.status_code != 200:
            return {"status": "failed", "job_id": job_id, "message": f"HTTP {resp.status_code}"}
        # One-shot worker: signal "running" + host so the caller triggers run_command.
        return {"status": "running", "job_id": job_id, "host": url}

    async def cancel_job(self, ctx: JobContext) -> dict:
        # No kill endpoint wired in this scope; worker jobs are one-shot.
        return {"status": "cancelled", "job_id": ctx.job_id, "backend": "gpu_worker"}
