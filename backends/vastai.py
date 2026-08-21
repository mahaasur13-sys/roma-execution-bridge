"""Vast.ai GPU backend for ROMA Execution Bridge.

Full lifecycle: search → rent → launch → monitor → bill → destroy.

Environment variables:
    VASTAI_API_KEY          – Vast.ai API key (required)
    VASTAI_DEFAULT_GPU      – Comma-separated GPU filter, e.g. "RTX_4090,A100"
    VASTAI_MAX_PRICE        – Max $/hour, e.g. "0.50"
    VASTAI_IMAGE            – Default Docker image
    VASTAI_DISK_GB          – Min disk space, default 20
    VASTAI_MAX_GPU_COUNT    – Max concurrent GPU instances, default 5
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from backends.base import BaseBackend, JobContext

logger = logging.getLogger("roma.backends.vastai")

VASTAI_API_BASE = "https://console.vast.ai/api/v0"


# ──────────────────────────────────────────────────────────────────────
# Data models
# ──────────────────────────────────────────────────────────────────────


@dataclass
class VastaiOffer:
    """Parsed Vast.ai machine offer."""

    instance_id: int
    hostname: str
    gpu_name: str
    gpu_count: int
    gpu_ram_mb: int
    cpu_cores: int
    ram_mb: int
    disk_gb: float
    price_per_hour: float
    score: float          # Vast.ai reliability score
    inet_down_mbps: float
    inet_up_mbps: float
    direct_port_count: int
    country: str = ""


@dataclass
class VastaiInstance:
    """Active rented instance on Vast.ai."""

    contract_id: int
    instance_id: int
    machine_id: int
    ssh_host: str
    ssh_port: int
    start_date: float  # unix timestamp
    actual_status: str
    image_runtype: str
    gpu_name: str
    price_per_hour: float

    @property
    def duration_seconds(self) -> float:
        return time.time() - self.start_date


# ──────────────────────────────────────────────────────────────────────
# Known GPU tiers (for smart selection)
# ──────────────────────────────────────────────────────────────────────

GPU_TIERS: dict[str, dict] = {
    "budget":    {"gpu_types": ["RTX_3060", "RTX_3070", "RTX_4060"],
                  "max_price": 0.30, "min_ram_mb": 8 * 1024},
    "standard":  {"gpu_types": ["RTX_3090", "RTX_4070", "RTX_4090"],
                  "max_price": 0.60, "min_ram_mb": 12 * 1024},
    "premium":   {"gpu_types": ["A100", "A6000", "H100", "A40", "L40S"],
                  "max_price": 2.00, "min_ram_mb": 24 * 1024},
}


# ──────────────────────────────────────────────────────────────────────
# VastaiBackend
# ──────────────────────────────────────────────────────────────────────


class VastaiBackend(BaseBackend):
    """Vast.ai execution backend — real GPU compute."""

    backend_name = "vastai"

    def __init__(self) -> None:
        self._api_key = os.getenv("VASTAI_API_KEY", "")
        self._default_gpu = os.getenv("VASTAI_DEFAULT_GPU", "RTX_4090")
        self._max_price = float(os.getenv("VASTAI_MAX_PRICE", "0.60"))
        self._default_image = os.getenv("VASTAI_IMAGE", "nvidia/cuda:12.1-runtime-ubuntu22.04")
        self._min_disk_gb = int(os.getenv("VASTAI_DISK_GB", "20"))
        self._max_gpu_count = int(os.getenv("VASTAI_MAX_GPU_COUNT", "5"))
        self._session = requests.Session()

        self._running_instances: dict[str, VastaiInstance] = {}
        self._job_instance_map: dict[str, int] = {}  # job_id → contract_id

        self._gpu_tier = self._resolve_gpu_tier()

    def _resolve_gpu_tier(self) -> dict:
        for tier_name, tier_cfg in GPU_TIERS.items():
            for gpu in self._default_gpu.split(","):
                gpu = gpu.strip()
                if gpu in tier_cfg["gpu_types"]:
                    logger.info("vastai.gpu_tier tier=%s gpu=%s", tier_name, gpu)
                    return tier_cfg
        return {"gpu_types": self._default_gpu.split(","),
                "max_price": self._max_price, "min_ram_mb": 4 * 1024}

    # ── public interface ──────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }

    def _api_get(self, path: str, params: dict | None = None) -> dict:
        url = f"{VASTAI_API_BASE}{path}"
        r = self._session.get(url, headers=self._headers(), params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def _api_put(self, path: str, json_data: dict | None = None) -> dict | bool:
        url = f"{VASTAI_API_BASE}{path}"
        r = self._session.put(url, headers=self._headers(), json=json_data, timeout=15)
        r.raise_for_status()
        body = r.json() if r.text else {}
        return body if isinstance(body, dict | bool) else {"raw": body}

    # ── instance management ────────────────────────────────────────

    def search_offers(self, gpu_filter: str | None = None,
                      min_ram_mb: int = 8 * 1024,
                      max_price: float | None = None,
                      min_disk_gb: float = 20.0,
                      min_inet_down: float = 100.0,
                      min_reliability: float = 0.80,
                      limit: int = 10) -> list[VastaiOffer]:
        """Search available instances on Vast.ai matching filters."""
        params: dict = {
            "type": "on-demand",
            "gpu_name": gpu_filter or self._default_gpu,
            "min_gpu_ram": min_ram_mb,
            "min_disk": min_disk_gb * 1024,  # Vast.ai expects MB
            "min_inet_down": min_inet_down,
            "reliability2": min_reliability,
            "limit": limit,
            "order": [("dph_total", "asc")],  # cheapest first
        }
        if max_price:
            params["max_dph"] = max_price * 1000  # Vast.ai uses dph = $/hour * 1000

        data = self._api_get("/bundles", params=params)
        offers_raw = data.get("offers", [])
        offers: list[VastaiOffer] = []

        for o in offers_raw:
            offers.append(VastaiOffer(
                instance_id=o["id"],
                hostname=o.get("hostname", ""),
                gpu_name=o.get("gpu_name", "unknown"),
                gpu_count=o.get("num_gpus", 1),
                gpu_ram_mb=o.get("gpu_ram", 0),
                cpu_cores=o.get("cpu_cores", 0),
                ram_mb=o.get("ram", 0),
                disk_gb=o.get("disk_space", 0) / 1024.0,
                price_per_hour=o.get("dph_total", 0) / 1000.0,
                score=o.get("score", 0),
                inet_down_mbps=o.get("inet_down", 0),
                inet_up_mbps=o.get("inet_up", 0),
                direct_port_count=o.get("direct_port_count", 0),
                country=o.get("geolocation", {}).get("country", ""),
            ))

        logger.info("vastai.search found=%d gpu=%s max_price=%s",
                     len(offers), gpu_filter or self._default_gpu, max_price)
        return sorted(offers, key=lambda o: o.price_per_hour)

    def rent_instance(self, offer: VastaiOffer, image: str | None = None,
                      env: dict | None = None, disk_gb: float = 20.0,
                      label: str = "") -> dict | None:
        """Rent a specific instance."""
        payload: dict = {
            "instance_id": offer.instance_id,
            "image": image or self._default_image,
            "env": env or {},
            "disk": disk_gb,
            "runtype": "ssh",  # direct SSH access
            "label": label or f"roma-{int(time.time())}",
            "onstart": "",  # no startup script — we handle via SSH
        }

        # For instances that support it, add direct ports
        if offer.direct_port_count > 0:
            payload["use_jupyter_lab"] = False

        result = self._api_put(f"/asks/{offer.instance_id}/", json_data=payload)

        if isinstance(result, dict):
            contract_id = result.get("contract_id") or result.get("new_contract")
            if contract_id:
                logger.info("vastai.rented instance=%d contract=%d price=%.4f/hr",
                            offer.instance_id, contract_id, offer.price_per_hour)
                return {
                    "contract_id": contract_id,
                    "instance_id": offer.instance_id,
                    "gpu_name": offer.gpu_name,
                    "price_per_hour": offer.price_per_hour,
                    "status": "provisioning",
                }

        logger.error("vastai.rent_failed instance=%d result=%s", offer.instance_id, result)
        return None

    def get_instances(self, contract_id: int | None = None) -> list[VastaiInstance]:
        """Get active/running instances."""
        data = self._api_get("/instances", params={"owner": "me"})
        instances: list[dict] = data.get("instances", [])
        result: list[VastaiInstance] = []

        for i in instances:
            status = i.get("actual_status", "unknown")
            inst = VastaiInstance(
                contract_id=i["id"],
                instance_id=i.get("machine_id", 0),
                machine_id=i.get("machine_id", 0),
                ssh_host=i.get("ssh_host", ""),
                ssh_port=i.get("ssh_port", 22),
                start_date=i.get("start_date", 0),
                actual_status=status,
                image_runtype=i.get("image_runtype", ""),
                gpu_name=i.get("gpu_name", ""),
                price_per_hour=i.get("dph_total", 0) / 1000.0,
            )
            if contract_id is None or inst.contract_id == contract_id:
                result.append(inst)

        return result

    def destroy_instance(self, instance_id: int) -> bool:
        """Destroy/stop a rented instance."""
        try:
            self._api_put(f"/instances/{instance_id}/destroy/")
            logger.info("vastai.destroyed instance=%d", instance_id)
            return True
        except Exception as e:
            logger.error("vastai.destroy_failed instance=%d error=%s", instance_id, e)
            return False

    def get_instance_logs(self, instance_id: int, limit: int = 50) -> str:
        """Get recent logs from instance (if supported)."""
        try:
            data = self._api_get(f"/instances/{instance_id}/logs/",
                                 params={"limit": limit})
            logs = data.get("logs", [])
            return "\n".join(logs[-limit:])
        except Exception as e:
            logger.warning("vastai.logs_failed instance=%d error=%s", instance_id, e)
            return f"[logs unavailable: {e}]"

    # ── job dispatch ───────────────────────────────────────────────

    async def dispatch(self, ctx: JobContext) -> dict:
        """Full dispatch: search → rent → launch on Vast.ai."""

        if not self.enabled:
            return {"status": "error", "message": "Vast.ai not configured: missing VASTAI_API_KEY"}

        gpu_filter = ctx.instance_type if ctx.instance_type != "any" else self._default_gpu
        max_price = self._gpu_tier.get("max_price", self._max_price)
        min_ram = self._gpu_tier.get("min_ram_mb", 8 * 1024)
        image = ctx.docker_image

        # Step 1: search
        offers = self.search_offers(
            gpu_filter=gpu_filter,
            max_price=max_price,
            min_ram_mb=min_ram,
            min_disk_gb=self._min_disk_gb,
            limit=5,
        )

        if not offers:
            logger.error("vastai.no_offers gpu=%s max_price=%s", gpu_filter, max_price)
            return {
                "status": "failed",
                "job_id": ctx.job_id,
                "message": f"No GPU instances found for {gpu_filter} ≤ ${max_price}/hr",
            }

        # Step 2: pick best offer (cheapest + best reliability)
        best = offers[0]  # already sorted by price
        logger.info("vastai.selected instance=%d gpu=%s score=%.2f price=%.4f/hr",
                     best.instance_id, best.gpu_name, best.score, best.price_per_hour)

        # Step 3: rent
        label = f"roma-{ctx.tenant_id[:12]}-{ctx.job_id[:8]}"
        rental = self.rent_instance(
            offer=best, image=image, disk_gb=self._min_disk_gb, label=label
        )

        if not rental:
            return {"status": "failed", "job_id": ctx.job_id,
                    "message": f"Failed to rent instance {best.instance_id}"}

        contract_id = rental["contract_id"]

        # Step 4: track
        self._job_instance_map[ctx.job_id] = contract_id

        return {
            "backend": "vastai",
            "status": "provisioning",
            "job_id": ctx.job_id,
            "contract_id": contract_id,
            "instance_id": best.instance_id,
            "gpu_name": best.gpu_name,
            "price_per_hour": best.price_per_hour,
            "ssh_host": "",  # filled after instance boots (via monitor)
            "estimated_boot_sec": 60,
        }

    async def get_status(self, job_id: str, instance_id: str | None = None) -> dict:
        """Get status of a job on Vast.ai."""
        contract_id = self._job_instance_map.get(job_id)
        if not contract_id and instance_id:
            contract_id = int(instance_id)

        if not contract_id:
            return {"status": "unknown", "job_id": job_id}

        instances = self.get_instances(contract_id=contract_id)
        if not instances:
            return {"status": "unknown", "job_id": job_id, "contract_id": contract_id}

        inst = instances[0]
        return {
            "status": inst.actual_status,
            "job_id": job_id,
            "contract_id": inst.contract_id,
            "ssh_host": inst.ssh_host,
            "ssh_port": inst.ssh_port,
            "duration_seconds": inst.duration_seconds,
            "gpu_name": inst.gpu_name,
            "price_per_hour": inst.price_per_hour,
            "cost_so_far": round(inst.duration_seconds / 3600 * inst.price_per_hour, 6),
        }

    async def cancel_job(self, ctx: JobContext) -> dict:
        """Cancel job and destroy the Vast.ai instance."""
        contract_id = self._job_instance_map.pop(ctx.job_id, None)

        if not contract_id:
            logger.warning("vastai.cancel.no_contract job=%s", ctx.job_id)
            return {"status": "noop", "job_id": ctx.job_id,
                    "message": "No Vast.ai contract found for this job"}

        # Get instance to log cost
        instances = self.get_instances(contract_id=contract_id)
        cost = 0.0
        duration_sec = 0.0
        if instances:
            inst = instances[0]
            duration_sec = inst.duration_seconds
            cost = round(duration_sec / 3600 * inst.price_per_hour, 6)

        self.destroy_instance(contract_id)

        return {
            "status": "cancelled",
            "job_id": ctx.job_id,
            "contract_id": contract_id,
            "duration_seconds": duration_sec,
            "cost_usd": cost,
            "backend": "vastai",
        }

    async def destroy_instance(self, instance_id: str) -> dict:
        success = self.destroy_instance(int(instance_id))
        return {"status": "destroyed" if success else "error", "instance_id": instance_id}

    # ── background monitor ──────────────────────────────────────────

    async def monitor_loop(self, complete_callback=None, interval: int = 15) -> None:
        """Background loop: poll Vast.ai instances, detect completions.

        Args:
            complete_callback: async func(job_id, ctx) called when job completes
            interval: polling interval in seconds
        """
        logger.info("vastai.monitor.start interval=%ds", interval)

        while True:
            try:
                instances = self.get_instances()
                now = datetime.now(timezone.utc)

                for inst in instances:
                    status = inst.actual_status

                    if status in ("stopped", "completed", "exited"):
                        job_id = self._find_job_by_contract(inst.contract_id)
                        if not job_id:
                            continue

                        duration_sec = inst.duration_seconds
                        cost_usd = round(duration_sec / 3600 * inst.price_per_hour, 6)

                        logger.info("vastai.job_complete job=%s contract=%d dur=%.1fs cost=%.6f",
                                    job_id, inst.contract_id, duration_sec, cost_usd)

                        ctx = JobContext(
                            job_id=job_id,
                            tenant_id="",  # filled by callback
                            plan_name="",
                            task="",
                            gpu_required=True,
                            instance_type=inst.gpu_name,
                            payload={"actual_gpu_seconds": duration_sec,
                                      "cost_usd": cost_usd,
                                      "gpu_name": inst.gpu_name},
                        )

                        if complete_callback:
                            try:
                                await complete_callback(job_id, ctx)
                            except Exception as e:
                                logger.error("vastai.complete_callback_failed job=%s: %s", job_id, e)

                        self._job_instance_map.pop(job_id, None)

                    # Check for "running" → ssh host newly available
                    elif status == "running" and inst.ssh_host:
                        job_id = self._find_job_by_contract(inst.contract_id)
                        if job_id:
                            pass  # Host available — state already tracked

            except Exception as e:
                logger.error("vastai.monitor.error: %s", e)

            await asyncio.sleep(interval)

    def _find_job_by_contract(self, contract_id: int) -> str | None:
        for jid, cid in self._job_instance_map.items():
            if cid == contract_id:
                return jid
        return None

    async def run_command(self, ctx: JobContext, command: str, timeout: int = 600) -> dict:
        """Выполняет команду на Vast.ai инстансе через /api/v0/commands/.
        
        Использует эндпоинт vast.ai для отправки команды на арендованный инстанс.
        Возвращает: {"status": "completed|failed|timeout", "output": "...", "cost": float}
        """
        contract_id = self._job_instance_map.get(ctx.job_id)
        if not contract_id:
            return {"status": "failed", "output": "", "error": "No contract for this job"}

        try:
            import base64
            
            payload = {
                "contract": contract_id,
                "cmd": command,
                "env": {},
                "timeout": timeout,
            }
            
            logger.info("vastai.run_command job=%s contract=%d cmd=%.80s", 
                         ctx.job_id, contract_id, command)
            
            result = self._api_post("/api/v0/commands/", json_data=payload)
            
            if isinstance(result, dict):
                output = result.get("output", "") or result.get("stdout", "")
                exit_code = result.get("exit_code", result.get("return_code", -1))
                status = "completed" if exit_code == 0 else "failed"
                
                logger.info("vastai.command_result job=%s status=%s exit=%d", 
                            ctx.job_id, status, exit_code)
                
                return {
                    "status": status,
                    "output": str(output),
                    "exit_code": exit_code,
                }
            
            return {"status": "failed", "output": str(result), "error": "API returned non-dict"}
            
        except Exception as e:
            logger.error("vastai.run_command_failed job=%s: %s", ctx.job_id, e)
            return {"status": "failed", "output": "", "error": str(e)}
