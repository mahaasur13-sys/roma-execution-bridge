"""Base backend interface for ROMA execution backends."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class JobContext:
    job_id: str
    tenant_id: str
    plan_name: str = "free"
    task: str = ""
    command: str = ""
    image: str = ""
    gpu_required: bool = False
    instance_type: str = "any"
    priority: int = 5
    backend: str = "local"
    timeout: int = 3600
    memory_gb: int = 8
    environment: dict = field(default_factory=dict)
    payload: dict = field(default_factory=dict)

    @property
    def docker_image(self) -> str:
        return self.image or os.getenv("VASTAI_IMAGE", "nvidia/cuda:12.1-runtime-ubuntu22.04")


class BaseBackend:
    """Abstract execution backend.

    Each backend (local, vastai, slurm, ray) implements this interface.
    """

    backend_name: str = "base"

    @property
    def enabled(self) -> bool:
        return False

    async def dispatch(self, ctx: JobContext) -> dict:
        """Launch job on this backend. Returns {'instance_id', 'status', ...}."""
        raise NotImplementedError

    async def get_status(self, job_id: str, instance_id: str | None = None) -> dict:
        """Return current job/instance status."""
        raise NotImplementedError

    async def cancel_job(self, ctx: JobContext) -> dict:
        """Cancel job and optionally destroy instance."""
        raise NotImplementedError

    async def get_logs(self, instance_id: str, lines: int = 50) -> str:
        """Get recent logs from instance."""
        raise NotImplementedError
