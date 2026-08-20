"""
ROMA WebSocket Server for GPU Workers.
Handles worker registration, heartbeat, job assignment, and status updates.
"""

import asyncio
import json
import logging
import os
from typing import Optional

logger = logging.getLogger("roma.ws")

WORKER_WS_ENABLED = os.environ.get("WORKER_WS_ENABLED", "false").lower() == "true"
WORKER_WS_PORT = int(os.environ.get("WORKER_WS_PORT", "8901"))
HEARTBEAT_TIMEOUT = int(os.environ.get("WORKER_HEARTBEAT_TIMEOUT", "60"))

# In-memory registry of connected workers: worker_id → websocket
connected_workers: dict[str, "WebSocket"] = {}


def _resolve_api_key(api_keys: dict, key: str) -> Optional[dict]:
    """Resolve API key to tenant info."""
    return api_keys.get(key)


async def _run_heartbeat_checker():
    """Periodic task to mark disconnected workers as offline."""
    import db
    while True:
        await asyncio.sleep(HEARTBEAT_TIMEOUT // 2)
        count = db.mark_offline_workers(HEARTBEAT_TIMEOUT)
        if count > 0:
            logger.info("Marked %d workers offline (timeout=%ds)", count, HEARTBEAT_TIMEOUT)


async def handle_worker(websocket, api_keys: dict):
    """Handle a single worker WebSocket connection."""
    worker_id = None
    tenant_id = None

    try:
        async for raw_message in websocket.iter_text():
            try:
                msg = json.loads(raw_message)
            except json.JSONDecodeError:
                await websocket.send(json.dumps({"type": "error", "message": "Invalid JSON"}))
                continue

            msg_type = msg.get("type", "")

            # --- REGISTRATION ---
            if msg_type == "register":
                api_key = msg.get("api_key", "")
                tenant_info = _resolve_api_key(api_keys, api_key)
                if not tenant_info:
                    await websocket.send(json.dumps({"type": "error", "message": "Unauthorized: invalid API key"}))
                    await websocket.close(code=4001)
                    return

                tenant_id = tenant_info["tenant_id"]
                worker_id = msg.get("worker_id", "")
                capabilities = msg.get("capabilities", {})

                if not worker_id:
                    await websocket.send(json.dumps({"type": "error", "message": "Missing worker_id"}))
                    continue

                import db
                db.register_worker(worker_id, tenant_id, capabilities)
                connected_workers[worker_id] = websocket

                await websocket.send(json.dumps({
                    "type": "registered",
                    "worker_id": worker_id,
                    "tenant_id": tenant_id,
                    "message": "Worker registered successfully",
                }))
                logger.info("Worker registered: %s (tenant=%s)", worker_id, tenant_id)

            # --- HEARTBEAT ---
            elif msg_type == "heartbeat":
                if not worker_id:
                    await websocket.send(json.dumps({"type": "error", "message": "Register first"}))
                    continue
                import db
                db.update_worker_heartbeat(worker_id)
                await websocket.send(json.dumps({"type": "heartbeat_ack"}))

            # --- JOB ACKNOWLEDGEMENT ---
            elif msg_type == "job_ack":
                job_id = msg.get("job_id", "")
                logger.info("Worker %s acknowledged job %s", worker_id, job_id)

            # --- STATUS UPDATE ---
            elif msg_type == "status_update":
                job_id = msg.get("job_id", "")
                status = msg.get("status", "unknown")
                output = msg.get("output", "")
                error = msg.get("error", "")
                # Update in-memory jobs dict (imported from main module context)
                # This is handled by the caller
                logger.info("Worker %s: job %s → %s", worker_id, job_id, status)
                await websocket.send(json.dumps({"type": "status_ack", "job_id": job_id, "status": status}))

            else:
                await websocket.send(json.dumps({"type": "error", "message": f"Unknown message type: {msg_type}"}))

    except Exception as e:
        logger.error("Worker %s connection error: %s", worker_id or "unknown", e)
    finally:
        if worker_id:
            connected_workers.pop(worker_id, None)
            logger.info("Worker disconnected: %s", worker_id)


async def assign_job_to_worker(worker_id: str, job_id: str, script: str, params: dict) -> bool:
    """Send job assignment to a connected worker. Returns True if sent successfully."""
    ws = connected_workers.get(worker_id)
    if not ws:
        logger.warning("Worker %s not connected — cannot assign job %s", worker_id, job_id)
        return False

    try:
        message = json.dumps({
            "type": "assign",
            "job_id": job_id,
            "script": script,
            "params": params,
        })
        await ws.send(message)

        import db
        db.assign_job_to_worker(worker_id, job_id)
        logger.info("Job %s assigned to worker %s", job_id, worker_id)
        return True
    except Exception as e:
        logger.error("Failed to assign job %s to worker %s: %s", job_id, worker_id, e)
        return False
