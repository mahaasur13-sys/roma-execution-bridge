"""Worker bridge for ROMA — connects jobs to WebSocket workers."""

import db


class WorkerBridge:
    """Locates and assigns workers to jobs."""

    def find_worker(self, tenant_id: str, instance_type: str = "any") -> dict | None:
        return db.find_worker(tenant_id, instance_type)

    def assign(self, worker_id: str, job_id: str) -> None:
        db.assign_job_to_worker(worker_id, job_id)

    def release(self, worker_id: str) -> None:
        db.release_worker(worker_id)

    def list_workers(self, tenant_id: str) -> list[dict]:
        return db.get_tenant_workers(tenant_id)


# Singleton
bridge = WorkerBridge()
