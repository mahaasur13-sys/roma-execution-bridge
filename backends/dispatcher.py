"""Backend registry — selects the right backend (local or vastai)."""
from __future__ import annotations

import os
import logging

from backends.base import BaseBackend, JobContext
from backends.local import LocalBackend

logger = logging.getLogger("roma.backend.dispatcher")
_backend: BaseBackend | None = None
_backend_name: str = ""


def get_backend() -> BaseBackend:
    """Lazy-load and cache the configured backend."""
    global _backend, _backend_name
    current = os.getenv("ROMA_EXECUTION_BACKEND", "local")

    if _backend is not None and _backend_name == current:
        return _backend

    _backend_name = current

    if current == "vastai":
        from backends.vastai import VastaiBackend
        _backend = VastaiBackend()
        if not _backend.enabled:
            logger.warning(
                "VASTAI selected but VAST_KEY (or VASTAI_API_KEY) not configured — falling back to local"
            )
            _backend = LocalBackend()
            _backend_name = "local"
    else:
        _backend = LocalBackend()

    return _backend


def list_backends() -> dict:
    """Return status of all available backends."""
    active = get_backend()
    backends = {}
    # local
    local = LocalBackend()
    backends["local"] = {"enabled": local.enabled, "active": _backend_name == "local"}
    # vastai
    try:
        from backends.vastai import VastaiBackend
        v = VastaiBackend()
        backends["vastai"] = {"enabled": v.enabled, "active": _backend_name == "vastai"}
    except Exception:
        backends["vastai"] = {"enabled": False, "active": False, "error": "import failed"}
    return backends


async def dispatch_job(job_id: str = "", tenant_id: str = "", payload: dict = None) -> dict:
    """Route job to active backend."""
    ctx = JobContext(
        job_id=job_id,
        tenant_id=tenant_id,
        payload=payload or {},
    )
    backend = get_backend()
    return await backend.dispatch(ctx)


async def backend_cancel_job(job_id: str, tenant_id: str = "") -> dict:
    """Cancel job on active backend (adapter for main.py)."""
    ctx = JobContext(job_id=job_id, tenant_id=tenant_id, payload={})
    backend = get_backend()
    return await backend.cancel_job(ctx)


async def get_job_status(job_id: str, instance_id: str | None = None) -> dict:
    """Check job status on active backend."""
    backend = get_backend()
    return await backend.get_status(job_id, instance_id)
