"""Local backend — current simulation mode (no real execution)."""
from __future__ import annotations

import logging

from backends.base import BaseBackend, JobContext

logger = logging.getLogger("roma.backends.local")


class LocalBackend(BaseBackend):
    """Local simulation backend — jobs stay 'queued' until manually completed.

    This is the current DEFAULT mode. No real execution happens.
    """

    backend_name = "local"

    @property
    def enabled(self) -> bool:
        return True

    async def dispatch(self, ctx: JobContext) -> dict:
        logger.info("local.dispatch job=%s tenant=%s task=%s", ctx.job_id, ctx.tenant_id, ctx.task[:60])
        return {
            "backend": "local",
            "status": "queued",
            "job_id": ctx.job_id,
            "message": "Job queued in local mode. Call POST /complete/{job_id} to mark complete.",
        }

    async def get_status(self, job_id: str, instance_id: str | None = None) -> dict:
        return {"status": "queued", "job_id": job_id}

    async def cancel_job(self, ctx: JobContext) -> dict:
        return {"status": "cancelled", "job_id": ctx.job_id, "backend": "local"}

    async def run_command(self, ctx: JobContext, command: str, timeout: int = 600) -> dict:
        """Выполняет команду локально (симуляция)."""
        import asyncio
        logger.info("local.run_command job=%s cmd=%.80s", ctx.job_id, command)
        # Симуляция выполнения
        await asyncio.sleep(0.5)
        return {"status": "completed", "output": f"OK: {command[:100]}", "exit_code": 0}
