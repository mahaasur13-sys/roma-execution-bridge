"""Backend preference for the hybrid model (phase 2). DISABLED by default.

ROMA_PREFER_OWN_HOSTS=1 AND ROMA_HOSTS_ENABLED=1 => verified, heartbeat-healthy
hosts are tried first. Vast/RunPod always remain in the list as fallback.
Any error while consulting the host registry degrades to marketplace-only.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("roma.scheduler.host_preference")

DEFAULT_FALLBACK = ("vastai", "runpod")


def prefer_own_hosts() -> bool:
    return (
        os.getenv("ROMA_PREFER_OWN_HOSTS", "0").strip() == "1" and os.getenv("ROMA_HOSTS_ENABLED", "0").strip() == "1"
    )


def backend_order(registry=None, fallback: tuple[str, ...] = DEFAULT_FALLBACK, min_vram_gb: float = 0.0) -> list[str]:
    order = list(fallback)
    if not prefer_own_hosts() or registry is None:
        return order
    try:
        hosts = [h for h in registry.schedulable_hosts() if h.vram_gb >= min_vram_gb]
    except Exception:  # fail-closed: never block dispatch on registry trouble
        logger.warning("host_preference.registry_error -> marketplace only")
        return order
    hosts.sort(key=lambda h: (-(h.benchmark_score or 0.0), h.host_id))
    return [f"host:{h.host_id}" for h in hosts] + order
