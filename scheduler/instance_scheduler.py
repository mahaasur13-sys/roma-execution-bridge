"""
ROMA Instance Scheduler — matches jobs to GPU workers by instance_type.
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger("roma.instance_scheduler")


def find_and_assign_worker(tenant_id: str, instance_type: str, job_id: str,
                           script: str, params: dict) -> Optional[str]:
    """
    Find an available worker matching instance_type and assign the job.
    Returns worker_id if assigned, None if no worker available.
    """
    import db
    worker = db.find_worker(tenant_id, instance_type)
    if not worker:
        logger.warning("No worker available: tenant=%s, instance_type=%s", tenant_id, instance_type)
        return None

    worker_id = worker["id"]
    # Synchronous DB update
    db.assign_job_to_worker(worker_id, job_id)

    # Try WebSocket assignment (fire-and-forget via asyncio background task)
    try:
        from scheduler.ws_server import assign_job_to_worker, connected_workers
        if worker_id in connected_workers:
            # Create task for async WebSocket send with error callback
            task = asyncio.create_task(assign_job_to_worker(worker_id, job_id, script, params))
            task.add_done_callback(
                lambda t: logger.error("WS assignment failed for worker %s: %s", worker_id, t.exception())
                if t.exception() else None
            )
        else:
            logger.warning("Worker %s found in DB but not connected via WebSocket — job %s queued", worker_id, job_id)
    except Exception as e:
        logger.error("WebSocket assignment error for worker %s: %s", worker_id, e)

    return worker_id


def release_worker(worker_id: str) -> None:
    """Release worker after job completion."""
    import db
    db.release_worker(worker_id)
    logger.info("Worker %s released", worker_id)
