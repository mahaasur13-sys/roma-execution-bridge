"""RunPod backend STUB (phase 1 aggregator). Mirrors the VastaiBackend interface.

Status: interface only. No network calls are made. Disabled unless BOTH
RUNPOD_API_KEY is set AND ROMA_RUNPOD_ENABLED=1; even then every operation
returns a fail-closed `not_implemented` result so nothing is rented or billed.
"""

from __future__ import annotations

import os

from backends.base import BaseBackend, JobContext


class RunpodBackend(BaseBackend):
    backend_name = "runpod"

    def __init__(self) -> None:
        self._has_key = bool((os.getenv("RUNPOD_API_KEY") or "").strip())
        self._flag = os.getenv("ROMA_RUNPOD_ENABLED", "0").strip() == "1"

    @property
    def enabled(self) -> bool:
        return self._has_key and self._flag

    def _refuse(self, op: str, **extra) -> dict:
        return {"status": "not_implemented", "backend": "runpod", "op": op,
                "enabled": self.enabled, **extra}

    def search_offers(self, *args, **kwargs) -> list:
        return []

    async def dispatch(self, ctx: JobContext) -> dict:
        return self._refuse("dispatch", job_id=ctx.job_id)

    async def get_status(self, job_id: str, instance_id: str | None = None) -> dict:
        return {"status": "unknown", "job_id": job_id, "backend": "runpod"}

    async def cancel_job(self, ctx: JobContext) -> dict:
        return self._refuse("cancel_job", job_id=ctx.job_id)

    async def get_logs(self, instance_id: str, lines: int = 50) -> str:
        return ""

    async def run_command(self, ctx: JobContext, command: str, timeout: int = 600) -> dict:
        return {"status": "not_implemented", "output": "", "exit_code": -1}
