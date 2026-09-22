#!/usr/bin/env python3
"""ROMA Scheduler — GPU-enabled task orchestration
Routes jobs to GPU workers based on cost/performance policy."""

import os
import asyncio
import logging
import shlex
from typing import Optional

from scheduler.gpu_policy_engine_v2 import GPUPolicyEngineV2
from gpu_worker.connector import get_gpu_connector
from cost.gate import EnterpriseDecisionGate as DecisionGate, GateResult
from cost.predictor import CostPredictor, UNKNOWN_TENANT
from queue_manager.queue_manager import QueueManager

logger = logging.getLogger("roma.scheduler")

# Minimal, explicit execution policy for local fallback execution (no shell).
ALLOWED_BINARIES = {"python", "python3"}


class ROMAGPUScheduler:
    def __init__(self):
        _qm = QueueManager()
        self.policy_engine = GPUPolicyEngineV2()
        self.gpu_connector = get_gpu_connector()

        # Register GPU nodes from connector
        try:
            worker_count = self.gpu_connector.get_worker_count()
            if worker_count > 0:
                for i in range(worker_count):
                    self.policy_engine.register_node(f"gpu-node-{i+1}")
            else:
                # Fallback: register default RTX 3060 node
                self.policy_engine.register_node("gpu-node-1")
        except Exception:
            self.policy_engine.register_node("gpu-node-1")
        try:
            self.cost_gate = DecisionGate()
        except Exception as e:
            logger.warning("DecisionGate init failed: %s, cost gate disabled", e)
            self.cost_gate = None
        self.predictor = CostPredictor()
        self.local_mode = os.getenv("ROMA_EXECUTION_MODE", "local")

    def route_job(self, job: dict) -> dict:
        gpu_required = job.get("gpu_required", True)

        # G-PRICING-TIER-PATH: тариф берётся из записи клиента внутри предиктора;
        # payload-поле tenant_tier больше не подаётся как авторитетный тариф.
        prediction = self.predictor.predict(
            task=job.get("task_type", "default"),
            gpu_required=gpu_required,
            plugin_type=job.get("plugin_type", "default"),
            tenant_id=job.get("tenant_id"),
            policy_engine=self.policy_engine,
        )

        if prediction.get("decision") == UNKNOWN_TENANT:
            # Клиент без записи — отдельный отказ: решение и цена по выдуманному
            # free-тарифу не считаются (не silent-FREE).
            return {
                "status": "rejected",
                "reason": UNKNOWN_TENANT,
                "detail": prediction.get("decision_reason", ""),
                "estimated_cost": None,
            }

        # R5b: контракт EnterpriseDecisionGate.evaluate(tenant_id, payload) -> GateDecision;
        # решение читается из полей dataclass, а не как из словаря ("REJECTED" контракт не отдаёт).
        payload = {
            "task": job.get("task_type", "default"),
            "gpu_required": gpu_required,
            "plugin_type": job.get("plan_tier", "PRO"),
        }
        if self.cost_gate is None:
            gate_decision, gate_allowed, gate_reason = (
                "disabled",
                True,
                "cost gate disabled",
            )
        else:
            gate_result = self.cost_gate.evaluate(
                job.get("tenant_id", "default"), payload
            )
            gate_decision = getattr(
                gate_result.result, "value", str(gate_result.result)
            )
            gate_allowed = gate_decision != GateResult.DENIED.value
            gate_reason = gate_result.reason

        if gpu_required and self.gpu_connector.is_available():
            if not gate_allowed:
                return {
                    "status": "rejected",
                    "reason": gate_reason,
                    "estimated_cost": prediction.get("estimated_cost", 0),
                }
            execution_target = "gpu_worker"
        else:
            execution_target = "local"

        return {
            "status": "queued",
            "execution_target": execution_target,
            "job_id": job.get("job_id"),
            "estimated_cost": prediction.get("estimated_cost", 0),
            "gate_decision": gate_decision,
        }

    async def execute_job(self, job: dict) -> dict:
        route = self.route_job(job)
        if route.get("status") == "rejected":
            return route

        if route["execution_target"] == "gpu_worker":
            gpu_job = {
                "job_id": job.get("job_id"),
                "command": job.get("command"),
                "image": job.get("docker_image"),
                "gpu": job.get("gpu", "any"),
                "memory": job.get("memory", "8GB"),
                "timeout": job.get("timeout", 3600),
                "environment": job.get("environment", {}),
            }
            result = await self.gpu_connector.execute(gpu_job)
            result["execution_target"] = "gpu_worker"
            return result
        else:
            return self._execute_local(job)

    def _execute_local(self, job: dict) -> dict:
        import subprocess

        try:
            command = job.get("command", "")
            argv = shlex.split(command) if command else []
            if not argv or os.path.basename(argv[0]) not in ALLOWED_BINARIES:
                return {
                    "status": "failed",
                    "error": "command not allowed (allowlist: python/python3)",
                    "execution_target": "local",
                }
            result = subprocess.run(
                argv,
                shell=False,
                capture_output=True,
                text=True,
                timeout=job.get("timeout", 300),
            )
            return {
                "status": "success" if result.returncode == 0 else "failed",
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
                "execution_target": "local",
                "duration_seconds": 0,
            }
        except Exception as e:
            return {"status": "failed", "error": str(e), "execution_target": "local"}

    def get_status(self) -> dict:
        return {
            "execution_mode": self.local_mode,
            "gpu_available": self.gpu_connector.is_available(),
            "gpu_worker_count": self.gpu_connector.get_worker_count(),
            "gpu_metrics": self.gpu_connector.get_metrics(),
            "policy_engine": self.policy_engine.get_status(),
        }


class ROMAJobExecutor:
    def __init__(self):
        self.scheduler = ROMAGPUScheduler()
        self.results: dict = {}
        self._job_ownership: dict = {}

    async def submit(self, job: dict) -> dict:
        import uuid

        job_id = job.get("job_id") or str(uuid.uuid4())
        job["job_id"] = job_id
        tenant_id = job.get("tenant_id", "unknown")
        self._job_ownership[job_id] = tenant_id
        route = self.scheduler.route_job(job)
        if route.get("status") == "rejected":
            return route

        result = await self.scheduler.execute_job(job)
        self.results[job_id] = result
        return result

    def get_result(self, job_id: str, tenant_id: str = None) -> Optional[dict]:
        if tenant_id:
            owner = self._job_ownership.get(job_id)
            if owner and owner != tenant_id:
                import logging

                logging.getLogger("roma.scheduler").warning(
                    "Tenant %s tried to access result of job %s owned by %s — denied",
                    tenant_id,
                    job_id,
                    owner,
                )
                return None
        return self.results.get(job_id)

    def get_metrics(self) -> dict:
        return {
            "results_tracked": len(self.results),
            "scheduler": self.scheduler.get_status(),
        }


_executor: Optional[ROMAJobExecutor] = None


def get_executor() -> ROMAJobExecutor:
    global _executor
    if _executor is None:
        _executor = ROMAJobExecutor()
    return _executor


if __name__ == "__main__":

    async def demo():
        executor = get_executor()
        print("=== ROMA GPU Scheduler ===")
        print(f"Status: {executor.get_metrics()}")

        # Демо-джобы ниже не несут tenant_id: после G-PRICING-TIER-PATH такой
        # джоб получает отказ UNKNOWN_TENANT (цена без записи клиента не считается).
        gpu_job = {
            "job_id": "demo-gpu-001",
            "task_type": "ml_training",
            "command": "echo 'GPU ready!' && nvidia-smi --query-gpu=name --format=csv,noheader",
            "gpu_required": True,
            "memory": "8GB",
            "timeout": 30,
            "tenant_tier": "PRO",
        }

        print("\n--- Submit GPU job ---")
        result = await executor.submit(gpu_job)
        print(f"Status: {result.get('status')}")
        print(f"Target: {result.get('execution_target', 'unknown')}")
        print(f"Worker: {result.get('worker_id', 'none')}")
        print(f"Output: {result.get('stdout', result.get('error', ''))[:200]}")

        local_job = {
            "job_id": "demo-local-001",
            "task_type": "data_prep",
            "command": "echo 'Local execution'",
            "gpu_required": False,
            "tenant_tier": "FREE",
        }

        print("\n--- Submit local job ---")
        result = await executor.submit(local_job)
        print(f"Status: {result.get('status')}")
        print(f"Target: {result.get('execution_target')}")

    asyncio.run(demo())
