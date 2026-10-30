"""B_active registry — selects the right backend (local or vastai)."""

from __future__ import annotations

import os
import logging

from backends.base import BaseBackend, JobContext
from backends.local import LocalBackend

logger = logging.getLogger("roma.backend.dispatcher")
_backend: BaseBackend | None = None
_backend_name: str = ""


def get_backend(name: str | None = None) -> BaseBackend:
    """Lazy-load and cache the configured backend."""
    global _backend, _backend_name
    current = name or os.getenv("ROMA_EXECUTION_BACKEND", "local") or "local"

    if _backend is not None and _backend_name == current:
        return _backend

    _backend_name = current

    if current == "vastai":
        from backends.vastai import VastaiBackend

        _backend = VastaiBackend()
        if not _backend.enabled:
            logger.warning("VASTAI selected but VAST_KEY (or VASTAI_API_KEY) not configured — falling back to local")
            _backend = LocalBackend()
            _backend_name = "local"
    elif current == "runpod" and os.getenv("ROMA_RUNPOD_ENABLED", "0").strip() == "1":
        # Phase-1 aggregator stub: interface only, every call is fail-closed `not_implemented`.
        from backends.runpod import RunpodBackend

        _backend = RunpodBackend()
        if not _backend.enabled:
            logger.warning("RUNPOD selected but RUNPOD_API_KEY not configured — falling back to local")
            _backend = LocalBackend()
            _backend_name = "local"
    elif current == "gpu_worker":
        from backends.gpu_worker_backend import GpuWorkerBackend

        _backend = GpuWorkerBackend()
    else:
        _backend = LocalBackend()

    return _backend


def list_backends() -> dict:
    """Return status of all available backends."""
    _active = get_backend()
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
        backends["vastai"] = {
            "enabled": False,
            "active": False,
            "error": "import failed",
        }
    # gpu_worker
    try:
        from backends.gpu_worker_backend import GpuWorkerBackend

        gw = GpuWorkerBackend()
        backends["gpu_worker"] = {
            "enabled": gw.enabled,
            "active": _backend_name == "gpu_worker",
        }
    except Exception:
        backends["gpu_worker"] = {
            "enabled": False,
            "active": False,
            "error": "import failed",
        }
    if os.getenv("ROMA_RUNPOD_ENABLED", "0").strip() == "1":
        try:
            from backends.runpod import RunpodBackend

            rp = RunpodBackend()
            backends["runpod"] = {"enabled": rp.enabled, "active": _backend_name == "runpod", "stub": True}
        except Exception:
            backends["runpod"] = {"enabled": False, "active": False, "error": "import failed"}
    return backends


_host_registry = None


def get_host_registry():
    """Process-local host registry (in-memory; PG persistence is a later step)."""
    global _host_registry
    if _host_registry is None:
        from gpu_worker.host_registry import HostRegistry

        _host_registry = HostRegistry()
    return _host_registry


def _routing_preference(p: dict) -> list[str] | None:
    """Advisory host-first order. None when flags are off (zero behaviour change)."""
    if not (os.getenv("ROMA_HOSTS_ENABLED", "0").strip() == "1"
            and os.getenv("ROMA_PREFER_OWN_HOSTS", "0").strip() == "1"):
        return None
    try:
        from scheduler.host_preference import backend_order

        return backend_order(get_host_registry(), min_vram_gb=float(p.get("min_vram_gb", 0) or 0))
    except Exception:
        logger.warning("dispatcher.host_preference_error -> ignored")
        return None


async def dispatch_job(job_id: str = "", tenant_id: str = "", payload: dict = None) -> dict:
    """Route job to active backend."""
    p = payload or {}
    ctx = JobContext(
        job_id=job_id,
        tenant_id=tenant_id,
        instance_type=p.get("instance_type", "any"),
        image=p.get("image", ""),
        gpu_required=p.get("gpu_required", False),
        priority=p.get("priority", 5),
        payload=p,
    )
    backend = get_backend(p.get("backend"))
    pref = _routing_preference(p)
    result = await backend.dispatch(ctx)
    if pref is not None and isinstance(result, dict):
        # Execution on ROMA hosts is NOT implemented yet: the job still runs on the
        # configured backend; the preferred order is only recorded for observability.
        result = {**result, "routing_preference": pref, "host_execution": "not_implemented"}
    return result


async def backend_cancel_job(job_id: str, tenant_id: str = "") -> dict:
    """Cancel job on active backend (adapter for main.py)."""
    ctx = JobContext(job_id=job_id, tenant_id=tenant_id, payload={})
    backend = get_backend()
    return await backend.cancel_job(ctx)


async def get_job_status(job_id: str, instance_id: str | None = None) -> dict:
    """Check job status on active backend."""
    backend = get_backend()
    return await backend.get_status(job_id, instance_id)
