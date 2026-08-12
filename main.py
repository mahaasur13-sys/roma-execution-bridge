"""
ROMA Execution Bridge – FastAPI + Pydantic v2
Multi-tenant execution platform with API-Key auth, tenant isolation, and billing.
"""

import json
import logging
import os
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import db

from fastapi import Depends, FastAPI, Header, HTTPException
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel, Field, ConfigDict
from starlette.requests import Request
from starlette.responses import Response


# ============================================
# JSON LOGGING
# ============================================

class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
        }
        for key in ("endpoint", "method", "status_code", "duration_ms", "api_key", "tenant_id", "job_id"):
            if hasattr(record, key):
                log_entry[key] = getattr(record, key)
        if record.exc_info and record.exc_info[0]:
            log_entry["error"] = record.getMessage()
            log_entry["traceback"] = traceback.format_exception(*record.exc_info)
        else:
            log_entry["message"] = record.getMessage()
        return json.dumps(log_entry, ensure_ascii=False)

_log_handler = logging.StreamHandler()
_log_handler.setFormatter(JSONFormatter())
_log_handler.setLevel(logging.INFO)

logger = logging.getLogger("roma")
logger.addHandler(_log_handler)
logger.setLevel(logging.INFO)
logger.propagate = False

def _mask_key(api_key: str | None) -> str | None:
    if not api_key:
        return None
    return api_key[:4] + "***" + api_key[-4:] if len(api_key) > 8 else "***"

# ============================================
# CONFIG LOADERS
# ============================================

CONFIG_DIR = Path(__file__).parent / "config"

def _load_json(filename: str) -> dict:
    path = CONFIG_DIR / filename
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}

def _save_json(filename: str, data: dict) -> None:
    path = CONFIG_DIR / filename
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

# ============================================
# API KEY AUTH + MULTI-TENANCY
# ============================================

def _load_api_keys() -> dict[str, dict]:
    return _load_json("api_keys.json")

API_KEYS: dict[str, dict] = _load_api_keys()

db.init_db()
db.seed_tenants(API_KEYS)

def verify_api_key(x_api_key: str = Header(None)) -> dict:
    """Validate API key and return tenant info: {tenant_id, name}."""
    if not x_api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing X-API-Key header. Request a key at https://roma-execution-bridge-asurdev.zocomputer.io",
        )
    if x_api_key not in API_KEYS:
        raise HTTPException(status_code=401, detail="Invalid API key")
    info = dict(API_KEYS[x_api_key]); info["api_key"] = x_api_key; return info

# ============================================
# PLANS & USAGE
# ============================================

PLANS: dict = _load_json("plans.json")
USAGE_FILE = "usage.json"

def _load_usage() -> dict:
    return _load_json(USAGE_FILE)

def _save_usage(usage: dict) -> None:
    _save_json(USAGE_FILE, usage)

def _get_tenant_usage(tenant_id: str) -> dict:
    usage = _load_usage()
    if tenant_id not in usage:
        usage[tenant_id] = {
            "total_jobs": 0,
            "total_gpu_seconds": 0,
            "last_updated": datetime.utcnow().isoformat(),
        }
        _save_usage(usage)
    return usage[tenant_id]

def _increment_usage(tenant_id: str, gpu_seconds: int = 0) -> dict:
    usage = _load_usage()
    if tenant_id not in usage:
        usage[tenant_id] = {"total_jobs": 0, "total_gpu_seconds": 0}
    usage[tenant_id]["total_jobs"] += 1
    usage[tenant_id]["total_gpu_seconds"] += gpu_seconds
    usage[tenant_id]["last_updated"] = datetime.utcnow().isoformat()
    _save_usage(usage)
    return usage[tenant_id]

def _check_limits(tenant_id: str) -> tuple[bool, str]:
    """Returns (allowed, reason). Checks subscription status + plan limits."""
    t = db.get_tenant(tenant_id)
    if not t:
        return False, f"Tenant '{tenant_id}' not found"

    sub_status = t.get("subscription_status", "inactive")
    plan_name = t.get("plan", "free")

    if sub_status == "inactive":
        return False, f"Tenant '{tenant_id}' has no active subscription. Subscribe at /billing/create-checkout-session"

    if sub_status == "past_due":
        return False, f"Payment past due. Update billing at /billing/create-checkout-session"

    plan = PLANS.get(plan_name, PLANS.get("free", {}))
    max_jobs = plan.get("max_jobs_per_month", 50)

    if max_jobs == -1:
        return True, ""

    tenant_usage = _get_tenant_usage(tenant_id)
    if tenant_usage["total_jobs"] >= max_jobs:
        return False, f"Plan '{plan_name}' limit reached: {tenant_usage['total_jobs']}/{max_jobs} jobs. Upgrade at /billing/create-checkout-session"
    return True, ""

# ============================================
# STRIPE — real integration with test-mode fallback
# ============================================

STRIPE_ENABLED = bool(os.environ.get("STRIPE_SECRET_KEY"))
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
WORKER_WS_ENABLED = os.environ.get("WORKER_WS_ENABLED", "false").lower() == "true"

STRIPE_PRICE_IDS = {
    "pro": os.environ.get("STRIPE_PRICE_PRO", ""),
    "enterprise": os.environ.get("STRIPE_PRICE_ENTERPRISE", ""),
}

if STRIPE_ENABLED:
    import stripe
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
else:
    stripe = None  # type: ignore[assignment]

# ============================================
# MODELS
# ============================================

class RomaTaskInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_default=True,
    )
    task: str = Field(..., min_length=1)
    gpu_required: bool = Field(default=False)
    priority: int = Field(default=5, ge=1, le=10)
    execution_mode: str = Field(default="k8s_job")
    backend: str = Field(default="local", pattern="^(local|slurm|ray)$")
    instance_type: str = Field(default="any", description="GPU type: any, RTX 3060, A100, H100")


class RomaTaskResponse(BaseModel):
    status: str
    job_id: str
    roma_dispatch: dict
    dag: list
    estimated_resources: dict
    gpu_required: bool
    tenant_id: str


class RomaStatusResponse(BaseModel):
    job_id: str
    status: str
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None


class CheckoutRequest(BaseModel):
    plan: str = Field(default="pro", pattern="^(free|pro|enterprise)$")


class CheckoutResponse(BaseModel):
    url: str
    plan: str
    mode: str


class UsageResponse(BaseModel):
    tenant_id: str
    plan: str
    usage: dict
    limits: dict

# ============================================
# APP
# ============================================

app = FastAPI(
    title="ROMA Execution Platform",
    version="1.0.0",
)

# ============================================
# PROMETHEUS METRICS (with tenant_id label)
# ============================================

roma_jobs_total = Counter("roma_jobs_total", "Total number of submitted jobs", ["tenant_id"])
roma_jobs_active = Gauge("roma_jobs_active", "Currently active jobs", ["tenant_id"])
roma_queue_depth = Gauge("roma_queue_depth", "Current queue depth", ["tenant_id"])
roma_requests_total = Counter("roma_requests_total", "Total HTTP requests", ["endpoint", "method", "status"])
roma_request_duration = Histogram("roma_request_duration_seconds", "Request duration in seconds", ["endpoint", "method"])

# ============================================
# MIDDLEWARE — structured logging + metrics
# ============================================

@app.middleware("http")
async def tracking_middleware(request: Request, call_next) -> Response:
    start = time.monotonic()
    api_key_raw = request.headers.get("X-API-Key")
    api_key_masked = _mask_key(api_key_raw)
    tenant_id = API_KEYS.get(api_key_raw, {}).get("tenant_id") if api_key_raw else None

    response = await call_next(request)

    duration_ms = round((time.monotonic() - start) * 1000, 2)
    endpoint = request.url.path
    method = request.method
    status = response.status_code

    roma_requests_total.labels(endpoint=endpoint, method=method, status=str(status)).inc()
    roma_request_duration.labels(endpoint=endpoint, method=method).observe(duration_ms / 1000)

    extra = {
        "endpoint": endpoint,
        "method": method,
        "status_code": status,
        "duration_ms": duration_ms,
        "api_key": api_key_masked,
        "tenant_id": tenant_id,
    }
    if status >= 500:
        logger.error(f"{method} {endpoint} → {status}", extra=extra)
    elif status >= 400:
        logger.warning(f"{method} {endpoint} → {status}", extra=extra)
    else:
        logger.info(f"{method} {endpoint} → {status}", extra=extra)

    return response


# ============================================
# IN-MEMORY STORAGE (tenant-isolated)
# ============================================

jobs: dict[str, dict] = {}
queue_depth: int = 0


# ============================================
# ENDPOINTS — Public
# ============================================

@app.get("/stats/daily")
async def daily_stats(request: Request):
    key_info = _resolve_api_key(request)
    if key_info is None:
        return JSONResponse(status_code=401, content={"detail": "Missing or invalid API key"})
    """Aggregated job counts + GPU hours for the last 7 days (tenant-isolated)."""
    tenant_id = key_info.get("tenant_id", "unknown")
    from datetime import datetime, timedelta

    today = datetime.utcnow().date()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    daily_jobs, daily_gpu = dict.fromkeys(dates, 0), dict.fromkeys(dates, 0.0)

    for job_id, job in jobs.items():
        if job.get("tenant_id") != tenant_id:
            continue
        try:
            jd = datetime.fromisoformat(job["submitted_at"]).date().isoformat()
        except Exception:
            continue
        if jd in daily_jobs:
            daily_jobs[jd] += 1
            daily_gpu[jd] += float(job.get("gpu_hours", 0) or 0)

    return {
        "tenant_id": tenant_id,
        "dates": dates,
        "jobs_count": [daily_jobs[d] for d in dates],
        "gpu_hours": [daily_gpu[d] for d in dates],
    }

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "queue_depth": queue_depth,
        "jobs": len(jobs),
        "billing": {"stripe_enabled": STRIPE_ENABLED},
    }


@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ============================================
# ENDPOINTS — Protected: Jobs
# ============================================

@app.post("/submit", response_model=RomaTaskResponse, status_code=202, dependencies=[Depends(verify_api_key)])
async def submit_task(payload: RomaTaskInput, request: Request, key_info: dict = Depends(verify_api_key)):
    global queue_depth
    tenant_id = key_info["tenant_id"]

    # Check plan limits
    allowed, reason = _check_limits(tenant_id)
    if not allowed:
        raise HTTPException(status_code=402, detail=reason)

    try:
        job_id = str(uuid.uuid4())
        queue_depth += 1
        roma_queue_depth.labels(tenant_id=tenant_id).set(queue_depth)

        job = {
            "status": "queued",
            "job_id": job_id,
            "tenant_id": tenant_id,
            "rom": f"rom://local/{job_id}",
            "submitted_at": datetime.utcnow().isoformat(),
            "payload": payload.model_dump(),
        }

        jobs[job_id] = job
        queue_depth -= 1
        roma_queue_depth.labels(tenant_id=tenant_id).set(queue_depth)
        roma_jobs_total.labels(tenant_id=tenant_id).inc()

        # Update usage
        gpu_sec = 300 if payload.gpu_required else 0  # estimate 5 min per GPU job
        _increment_usage(tenant_id, gpu_sec)

        tenant_jobs = [j for j in jobs.values() if j.get("tenant_id") == tenant_id]
        roma_jobs_active.labels(tenant_id=tenant_id).set(len(tenant_jobs))

        return RomaTaskResponse(
            status="queued",
            job_id=job_id,
            tenant_id=tenant_id,
            roma_dispatch={"protocol": "rom", "target": job["rom"]},
            dag=["validate", "dispatch", "execute", "commit"],
            estimated_resources={"cpu_cores": 2, "memory_mb": 512, "gpu": 0},
            gpu_required=payload.gpu_required,
        )

    except Exception as e:
        queue_depth -= 1
        roma_queue_depth.labels(tenant_id=tenant_id).set(queue_depth)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/status/{job_id}", response_model=RomaStatusResponse, dependencies=[Depends(verify_api_key)])
async def get_status(job_id: str, key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]

    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]
    if job.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="Job not found")

    return RomaStatusResponse(
        job_id=job_id,
        status=job["status"],
        created_at=job["submitted_at"],
    )


@app.post("/cancel/{job_id}", dependencies=[Depends(verify_api_key)])
async def cancel_job(job_id: str, key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]

    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]
    if job.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="Job not found")

    jobs[job_id]["status"] = "cancelled"
    return {"status": "cancelled", "job_id": job_id}


@app.get("/jobs", dependencies=[Depends(verify_api_key)])
async def list_jobs(key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]
    my_jobs = [j for j in jobs.values() if j.get("tenant_id") == tenant_id]
    return {
        "rom_version": "1.0.0",
        "tenant_id": tenant_id,
        "queue": len(my_jobs),
        "jobs": my_jobs[-10:],
        "execution_modes": ["k8s_job", "k8s_persistent", "atom_cluster", "batch"],
    }


@app.post("/submit/cluster", status_code=202, dependencies=[Depends(verify_api_key)])
async def submit_atom_cluster(payload: dict, key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]
    cluster_spec = payload.get("cluster_spec", {})
    cluster_name = cluster_spec.get("name", "default")

    job_id = str(uuid.uuid4())
    job = {
        "status": "atom_cluster_managed",
        "job_id": job_id,
        "tenant_id": tenant_id,
        "cluster_name": cluster_name,
        "execution_mode": "atom_cluster",
        "atom_cluster": {"name": cluster_name, "managed": True, "nodes": cluster_spec.get("nodes", 1)},
    }
    jobs[job_id] = job
    return job


# ============================================
# ENDPOINTS — Billing & Usage
# ============================================

@app.get("/usage", dependencies=[Depends(verify_api_key)])
async def get_usage(key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]
    t = db.get_tenant(tenant_id)
    plan_name = t["plan"] if t else key_info.get("plan", "free")
    plan = PLANS.get(plan_name, PLANS.get("free", {}))
    usage_data = _get_tenant_usage(tenant_id)
    max_jobs = plan.get("max_jobs_per_month", 50)
    sub_status = t.get("subscription_status", "inactive") if t else "inactive"

    return {
        "tenant_id": tenant_id,
        "plan": plan_name,
        "subscription_status": sub_status,
        "usage": usage_data,
        "limits": {
            "max_jobs_per_month": max_jobs,
            "max_jobs_per_month_display": "unlimited" if max_jobs == -1 else str(max_jobs),
        },
    }


@app.post("/billing/create-checkout-session", dependencies=[Depends(verify_api_key)])
async def create_checkout_session(body: CheckoutRequest, key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]
    plan_name = body.plan
    plan = PLANS.get(plan_name, PLANS.get("pro", {}))

    # Free plan — activate immediately, no Stripe needed
    if plan_name == "free":
        db.update_tenant_subscription(tenant_id, "", "", "active", "free", None)
        return {
            "status": "subscribed",
            "plan": "free",
            "tenant_id": tenant_id,
            "message": "Free plan activated — no payment required.",
        }

    if not STRIPE_ENABLED or not STRIPE_PRICE_IDS.get(plan_name):
        return {
            "status": "billing_disabled",
            "message": (
                "Stripe is not fully configured. To enable:\n"
                "1. Add STRIPE_SECRET_KEY to Zo Secrets\n"
                "2. Add STRIPE_PRICE_PRO / STRIPE_PRICE_ENTERPRISE with Stripe Price IDs\n"
                "3. Restart the ROMA service\n\n"
                f"Selected plan: {plan_name} (${plan.get('price_monthly', 0)}/month)"
            ),
            "plan": plan_name,
            "tenant_id": tenant_id,
        }

    try:
        price_id = STRIPE_PRICE_IDS[plan_name]
        success_url = f"https://roma-execution-bridge-asurdev.zocomputer.io/dashboard?api_key={key_info['api_key'] or ''}&session_id={{CHECKOUT_SESSION_ID}}"
        cancel_url = f"https://roma-execution-bridge-asurdev.zocomputer.io/dashboard?api_key={key_info['api_key'] or ''}"

        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                "tenant_id": tenant_id,
                "plan": plan_name,
            },
        )
        return {
            "status": "checkout_created",
            "session_id": session.id,
            "url": session.url,
            "plan": plan_name,
            "tenant_id": tenant_id,
        }
    except Exception as e:
        logger.error(f"Stripe checkout error for {tenant_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")


# ============================================

# ============================================
# DEMOS — ready-to-run tasks
# ============================================

DEMOS = {
    "demo-pytorch-train": {
        "task": "Train ResNet-18 on CIFAR-10 (dummy — 2 epochs)",
        "gpu_required": True,
        "priority": 8,
        "execution_mode": "k8s_job",
        "description": "Simple PyTorch model training with gradient descent.",
        "estimated_time": "~2 min",
    },
    "demo-inference": {
        "task": "Run inference — BERT sentiment classifier on sample reviews",
        "gpu_required": True,
        "priority": 6,
        "execution_mode": "k8s_job",
        "description": "Batch inference using a pre-trained BERT model.",
        "estimated_time": "~30 sec",
    },
    "demo-batch-processing": {
        "task": "Batch process 1000 images — resize + normalize",
        "gpu_required": False,
        "priority": 5,
        "execution_mode": "k8s_job",
        "description": "Mass image processing pipeline — no GPU needed.",
        "estimated_time": "~1 min",
    },
    "demo-gpu-benchmark": {
        "task": "GPU benchmark — matrix multiplication 4096^2",
        "gpu_required": True,
        "priority": 10,
        "execution_mode": "k8s_job",
        "description": "Raw GPU compute test — measures FLOPS.",
        "estimated_time": "~15 sec",
    },
    "demo-hello-world": {
        "task": "Hello World — verify connectivity + task submission",
        "gpu_required": False,
        "priority": 1,
        "execution_mode": "k8s_job",
        "description": "Simple smoke-test job — prints timestamp and hostname.",
        "estimated_time": "~5 sec",
    },
}


@app.post("/demo/{demo_name}")
async def run_demo(demo_name: str, key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]
    if demo_name not in DEMOS:
        raise HTTPException(status_code=404, detail=f"Demo not found: {demo_name}. Available: {list(DEMOS.keys())}")
    demo = DEMOS[demo_name]
    payload = RomaTaskInput(
        task=demo["task"],
        gpu_required=demo["gpu_required"],
        priority=demo["priority"],
        execution_mode=demo["execution_mode"],
    )
    return await submit_job(payload, key_info)
# ============================================
# SLURM INTEGRATION ENDPOINTS
# ============================================

from scheduler.slurm_plugin import slurm as slurm_plugin


@app.get("/slurm/status/{slurm_job_id}", dependencies=[Depends(verify_api_key)])
async def slurm_status(slurm_job_id: str, key_info: dict = Depends(verify_api_key)):
    """Get Slurm job status via sacct/squeue."""
    result = slurm_plugin.get_status(slurm_job_id)
    return result


@app.post("/slurm/cancel/{slurm_job_id}", dependencies=[Depends(verify_api_key)])
async def slurm_cancel(slurm_job_id: str, key_info: dict = Depends(verify_api_key)):
    """Cancel Slurm job via scancel."""
    result = slurm_plugin.cancel(slurm_job_id)
    return result


# ============================================
# ENDPOINTS — Worker Management
# ============================================


@app.get("/workers", dependencies=[Depends(verify_api_key)])
async def list_workers(key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]
    workers = db.get_tenant_workers(tenant_id)
    return {"workers": workers, "count": len(workers)}

@app.get("/workers/{worker_id}", dependencies=[Depends(verify_api_key)])
async def get_worker(worker_id: str, key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]
    w = db.get_worker_by_id(worker_id)
    if not w or w.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="Worker not found")
    return w

@app.post("/workers/{worker_id}/drain", dependencies=[Depends(verify_api_key)])
async def drain_worker(worker_id: str, key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]
    w = db.get_worker_by_id(worker_id)
    if not w or w.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="Worker not found")
    db.drain_worker(worker_id)
    return {"status": "draining", "worker_id": worker_id}

# ENTRY POINT
# ============================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8899)


# ============================================
# ============================================
# ENDPOINTS — Workers
# ============================================

@app.get("/workers", dependencies=[Depends(verify_api_key)])
async def list_workers(key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]
    workers = db.get_tenant_workers(tenant_id)
    return {"workers": workers, "count": len(workers)}

@app.get("/workers/{worker_id}", dependencies=[Depends(verify_api_key)])
async def get_worker(worker_id: str, key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]
    w = db.get_worker_by_id(worker_id)
    if not w or w["tenant_id"] != tenant_id:
        raise HTTPException(status_code=404, detail="Worker not found")
    return w

@app.post("/workers/{worker_id}/drain", dependencies=[Depends(verify_api_key)])
async def drain_worker(worker_id: str, key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]
    w = db.get_worker_by_id(worker_id)
    if not w or w["tenant_id"] != tenant_id:
        raise HTTPException(status_code=404, detail="Worker not found")
    db.drain_worker(worker_id)
    return {"status": "draining", "worker_id": worker_id}

# ============================================
# WEBSOCKET — Worker Registration
# ============================================

from fastapi import WebSocket, WebSocketDisconnect
import asyncio

active_ws_workers: dict[str, WebSocket] = {}

@app.websocket("/ws/worker")
async def ws_worker(ws: WebSocket):
    await ws.accept()
    worker_id = None
    tenant_id = None
    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type")
            if msg_type == "register":
                worker_id = data["worker_id"]
                api_key = data.get("api_key", "")
                capabilities = data.get("capabilities", {})
                if api_key not in API_KEYS:
                    await ws.send_json({"type": "error", "message": "Invalid API key"})
                    await ws.close(code=4001)
                    return
                tenant_id = API_KEYS[api_key]["tenant_id"]
                db.register_worker(worker_id, tenant_id, capabilities)
                active_ws_workers[worker_id] = ws
                await ws.send_json({"type": "registered", "worker_id": worker_id, "tenant_id": tenant_id})
                logger.info("worker_registered", extra={"tenant_id": tenant_id, "worker_id": worker_id})
            elif msg_type == "heartbeat":
                db.update_worker_heartbeat(worker_id)
                await ws.send_json({"type": "heartbeat_ack", "worker_id": worker_id})
            elif msg_type == "status_update":
                job_id = data.get("job_id")
                status = data.get("status")
                if job_id in jobs:
                    jobs[job_id]["status"] = status
                    if status in ("completed", "failed"):
                        db.release_worker(worker_id)
                        if "output" in data:
                            jobs[job_id]["output"] = data["output"]
                        if "error" in data:
                            jobs[job_id]["error"] = data["error"]
                logger.info("worker_status_update", extra={"tenant_id": tenant_id, "worker_id": worker_id, "job_id": job_id, "status": status})
    except WebSocketDisconnect:
        logger.info("worker_disconnected", extra={"tenant_id": tenant_id, "worker_id": worker_id})
    except Exception as e:
        logger.error(f"ws_worker error: {e}", extra={"tenant_id": tenant_id, "worker_id": worker_id})
    finally:
        if worker_id:
            active_ws_workers.pop(worker_id, None)


# DASHBOARD — HTML page (browser-friendly)
# ============================================


# ============================================
# BROWSER AUTH — cookie-based sessions
# ============================================

from auth.sessions import create_session, get_session, delete_session
from starlette.responses import RedirectResponse

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ROMA — Login</title>
<style>
* { margin:0; padding:0; box-sizing:border-box }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#0f1117; color:#e5e7eb; display:flex; align-items:center; justify-content:center; min-height:100vh }
.card { background:#161b22; border:1px solid #30363d; border-radius:12px; padding:40px; max-width:420px; width:100% }
h1 { font-size:24px; margin-bottom:8px; color:#f9fafb }
p { color:#8b949e; font-size:14px; margin-bottom:24px }
input { width:100%; padding:10px 14px; background:#0d1117; border:1px solid #30363d; border-radius:8px; color:#e5e7eb; font-size:15px; margin-bottom:16px; outline:none }
input:focus { border-color:#3b82f6 }
button { width:100%; padding:10px; background:#238636; border:none; border-radius:8px; color:#fff; font-size:15px; cursor:pointer; font-weight:600 }
button:hover { background:#2ea043 }
.error { background:rgba(239,68,68,0.1); border:1px solid #ef4444; border-radius:8px; padding:12px; color:#ef4444; font-size:14px; margin-bottom:16px }
.hint { font-size:12px; color:#6b7280; margin-top:16px; text-align:center }
.hint code { background:#1f2937; padding:1px 6px; border-radius:4px }
</style>
</head>
<body>
<div class="card">
    <h1>⚡ ROMA Execution Bridge</h1>
    <p>Enter your API key to access the dashboard.</p>
    <form method="POST" action="/auth/login">
        <input type="text" name="api_key" placeholder="roma-demo-key-2026" autofocus required>
        <button type="submit">Sign In</button>
    </form>
    <div class="hint">Test key: <code>roma-demo-key-2026</code></div>
</div>
</body>
</html>"""


@app.get("/auth/login")
async def login_page(request: Request):
    """Show login form."""
    # If already logged in, redirect to dashboard
    session_id = request.cookies.get("session_id")
    if session_id and get_session(session_id):
        return RedirectResponse(url="/dashboard", status_code=302)
    return Response(content=LOGIN_PAGE, media_type="text/html")


@app.post("/auth/login")
async def login(request: Request):
    """Process login form submission."""
    form = await request.form()
    api_key = form.get("api_key", "")
    if api_key not in API_KEYS:
        # Show login page with error
        error_html = LOGIN_PAGE.replace("</form>", '<div class="error">Invalid API key. Try <code>roma-demo-key-2026</code></div></form>')
        return Response(content=error_html, media_type="text/html", status_code=401)

    info = API_KEYS[api_key]
    session_id = create_session(info["tenant_id"], api_key)

    resp = RedirectResponse(url="/dashboard", status_code=302)
    resp.set_cookie(
        "session_id", session_id,
        httponly=True, max_age=3600, samesite="lax",
    )
    return resp


@app.get("/auth/logout")
async def logout(request: Request):
    """Clear session and redirect to login."""
    session_id = request.cookies.get("session_id")
    if session_id:
        delete_session(session_id)
    resp = RedirectResponse(url="/auth/login", status_code=302)
    resp.delete_cookie("session_id")
    return resp

def _resolve_api_key(request: Request) -> dict | None:
    """Try header first, then query param (for browser access)."""
    key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
    if not key or key not in API_KEYS:
        return None
    return API_KEYS[key]

def _render_dashboard(tenant_id: str, plan_name: str, api_key: str) -> str:
    usage_data = _get_tenant_usage(tenant_id)
    plan = PLANS.get(plan_name, PLANS.get("free", {}))
    max_jobs = plan.get("max_jobs_per_month", 50)
    used = usage_data["total_jobs"]
    pct = min(100, round(used / max_jobs * 100, 1)) if max_jobs > 0 else 0
    limit_display = "∞" if max_jobs == -1 else str(max_jobs)
    bar_color = "#22c55e" if pct < 60 else "#f59e0b" if pct < 85 else "#ef4444"

    my_jobs = [j for j in jobs.values() if j.get("tenant_id") == tenant_id]
    recent = my_jobs[-10:][::-1]  # newest first

    jobs_html = ""
    if recent:
        for j in recent:
            status_cls = {"queued": "#3b82f6", "cancelled": "#9ca3af", "completed": "#22c55e"}.get(j["status"], "#6b7280")
            jobs_html += f"""<tr>
                <td style="font-family:monospace;font-size:13px">{j['job_id'][:8]}...</td>
                <td><span style="background:{status_cls};color:#fff;padding:2px 8px;border-radius:10px;font-size:12px">{j['status']}</span></td>
                <td style="font-size:13px">{j.get('submitted_at','—')[:19]}</td>
                <td><a href="/status/{j['job_id']}?api_key=' + api_key + '" style="color:#3b82f6">details →</a></td>
            </tr>"""
    else:
        jobs_html = '<tr><td colspan="4" style="color:#9ca3af;padding:20px">No jobs yet. Send your first task!</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ROMA Dashboard — {tenant_id}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background:#0f1117; color:#e5e7eb; min-height:100vh }}
.container {{ max-width:900px; margin:0 auto; padding:24px 16px }}
header {{ padding:24px 0; border-bottom:1px solid #1f2937; margin-bottom:28px }}
header h1 {{ font-size:26px; font-weight:700; color:#f9fafb }}
header .sub {{ color:#9ca3af; font-size:14px; margin-top:4px }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(260px,1fr)); gap:16px; margin-bottom:28px }}
.card {{ background:#161b22; border:1px solid #30363d; border-radius:12px; padding:20px }}
.card h3 {{ font-size:13px; font-weight:600; color:#8b949e; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:12px }}
.card .value {{ font-size:24px; font-weight:700; color:#f0f6fc }}
.card .sub {{ font-size:13px; color:#8b949e; margin-top:4px }}
.bar-container {{ background:#21262d
.charts-row {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; margin-bottom:28px }}
@media(max-width:768px) {{ .charts-row {{ grid-template-columns:1fr }} }}
.chart-box {{ background:#161b22; border:1px solid #30363d; border-radius:12px; padding:16px; height:300px }}; border-radius:8px; height:10px; margin-top:10px; overflow:hidden }}
.bar-fill {{ background:{bar_color}; height:100%; width:{pct}%; border-radius:8px; transition:width 0.4s }}
table {{ width:100%; border-collapse:collapse; margin-top:8px }}
th {{ text-align:left; padding:10px 12px; font-size:12px; font-weight:600; color:#8b949e; text-transform:uppercase; border-bottom:1px solid #30363d }}
td {{ padding:10px 12px; border-bottom:1px solid #1f2937 }}
th:last-child, td:last-child {{ text-align:right }}
.actions {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:16px }}
.actions a {{ color:#3b82f6; text-decoration:none; font-size:14px; padding:6px 14px; border:1px solid #30363d; border-radius:6px; transition:all 0.15s }}
.actions a:hover {{ border-color:#3b82f6; background:rgba(59,130,246,0.1) }}
footer {{ margin-top:40px; padding-top:20px; border-top:1px solid #1f2937; color:#6b7280; font-size:13px; display:flex; justify-content:space-between; flex-wrap:wrap; gap:8px }}
footer .dot {{ display:inline-block; width:7px; height:7px; border-radius:50%; background:#22c55e; margin-right:6px }}
.status-badge {{ background:rgba(34,197,94,0.15); color:#22c55e; padding:3px 10px; border-radius:10px; font-size:12px }}
</style>
</head>
<body>
<div class="container">

<header>
    <h1>⚡ ROMA Execution Bridge</h1>
    <div class="sub">Dashboard for <strong>{tenant_id}</strong> · <span class="status-badge">● connected</span></div>
</header>

<div class="cards">
    <div class="card">
        <h3>Account</h3>
        <div class="value">{tenant_id}</div>
        <div class="sub">Plan: <strong>{plan.get('name', plan_name)}</strong> (${plan.get('price_monthly', 0)}/month)</div>
    </div>
    <div class="card">
        <h3>Usage</h3>
        <div class="value">{used} / {limit_display}</div>
        <div class="sub">jobs this month</div>
        <div class="bar-container"><div class="bar-fill"></div></div>
    </div>
    <div class="card">
        <h3>Queue</h3>
        <div class="value">{queue_depth}</div>
        <div class="sub">jobs waiting</div>
    </div>
</div>

<div class="card" style="margin-bottom:28px">
    <h3>Recent Jobs</h3>
    <table>
        <thead><tr><th>Job ID</th><th>Status</th><th>Created</th><th></th></tr></thead>
        <tbody>{jobs_html}</tbody>
    </table>
</div>

<div class="card">
    <h3>Quick Actions</h3>
    <div class="actions">
        <a href="/health">♥ /health</a>
        <a href="/metrics">📊 /metrics</a>
        <a href="/usage?api_key=' + api_key + '">📈 /usage</a>
        <a href="/jobs?api_key=' + api_key + '">📋 /jobs</a>
        <a href="/docs/quickstart.md">📖 Quickstart</a>
    </div>
</div>


    <!-- 📊 USAGE CHARTS -->
    <h3 style="margin-bottom:16px">📊 Usage (Last 7 Days)</h3>
    <div class="charts-row">
        <div class="chart-box"><canvas id="jobsChart"></canvas></div>
        <div class="chart-box"><canvas id="gpuChart"></canvas></div>
    </div>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <script>
    (function() {{
        var apiKey = "";
        try {{ apiKey = new URLSearchParams(window.location.search).get("api_key") || ""; }} catch(e) {{}}
        var xhr = new XMLHttpRequest();
        xhr.open("GET", "/stats/daily?api_key=" + encodeURIComponent(apiKey), true);
        xhr.onload = function() {{
            if (xhr.status !== 200) return;
            var data = JSON.parse(xhr.responseText);
            var dates = data.dates || [];
            var jobsCount = data.jobs_count || [];
            var gpuHours = data.gpu_hours || [];
            var ctx1 = document.getElementById("jobsChart").getContext("2d");
            new Chart(ctx1, {{
                type: "bar",
                data: {{
                    labels: dates,
                    datasets: [{{
                        label: "Jobs",
                        data: jobsCount,
                        backgroundColor: "rgba(99,102,241,0.4)",
                        borderColor: "#6366f1",
                        borderWidth: 1
                    }}]
                }},
                options: {{
                    responsive: true, maintainAspectRatio: false,
                    plugins: {{
                        title: {{ display: true, text: "Jobs per Day", color: "#f9fafb", font: {{ size: 14 }} }},
                        legend: {{ labels: {{ color: "#9ca3af" }} }}
                    }},
                    scales: {{
                        x: {{ ticks: {{ color: "#9ca3af" }}, grid: {{ color: "#1f2937" }} }},
                        y: {{ ticks: {{ color: "#9ca3af", beginAtZero: true }}, grid: {{ color: "#1f2937" }} }}
                    }}
                }}
            }});
            var ctx2 = document.getElementById("gpuChart").getContext("2d");
            new Chart(ctx2, {{
                type: "bar",
                data: {{
                    labels: dates,
                    datasets: [{{
                        label: "GPU Hours",
                        data: gpuHours,
                        backgroundColor: "rgba(34,197,94,0.4)",
                        borderColor: "#22c55e",
                        borderWidth: 1
                    }}]
                }},
                options: {{
                    responsive: true, maintainAspectRatio: false,
                    plugins: {{
                        title: {{ display: true, text: "GPU Hours per Day", color: "#f9fafb", font: {{ size: 14 }} }},
                        legend: {{ labels: {{ color: "#9ca3af" }} }}
                    }},
                    scales: {{
                        x: {{ ticks: {{ color: "#9ca3af" }}, grid: {{ color: "#1f2937" }} }},
                        y: {{ ticks: {{ color: "#9ca3af", beginAtZero: true }}, grid: {{ color: "#1f2937" }} }}
                    }}
                }}
            }});
        }};
        xhr.send();
    }})();
    </script>

<footer>
    <span><span class="dot"></span> ROMA v1.0.0 — Phase 0 Pre-Launch</span>
    <span>Uptime: since restart</span>

<div class="charts-row hidden" id="dashboard-chart-box">
    <div class="chart-box"><canvas id="chartJobs"></canvas></div>
    <div class="chart-box"><canvas id="chartGPU"></canvas></div>
</div>
<script>
(async function() {{
    const key = new URLSearchParams(window.location.search).get("api_key") || "";
    let rows = [];
    try {{
        const r = await fetch("/stats/daily?api_key=" + encodeURIComponent(key));
        if (r.ok) {{
            const d = await r.json();
            rows = d.dates.map((date, i) => ({{ date, jobs: d.jobs_count[i], gpu: d.gpu_hours[i] }}));
        }}
    }} catch(_) {{}}
    if (!rows.length) return;
    document.getElementById("dashboard-chart-box").classList.remove("hidden");

    new Chart(document.getElementById("chartJobs"), {{
        type: "bar", data: {{ labels: rows.map(r => r.date), datasets: [{{ label: "Jobs", data: rows.map(r => r.jobs), backgroundColor: "#3b82f6" }}] }},
        options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }} }}
    }});

    new Chart(document.getElementById("chartGPU"), {{
        type: "bar", data: {{ labels: rows.map(r => r.date), datasets: [{{ label: "GPU Hours", data: rows.map(r => r.gpu), backgroundColor: "#22c55e" }}] }},
        options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }} }}
    }});
}})();
</script>

</footer>

</div>
</body>
</html>"""

@app.get("/dashboard")
async def dashboard(request: Request):
    # 1. Check session cookie first
    session_id = request.cookies.get("session_id")
    if session_id:
        sess = get_session(session_id)
        if sess:
            tenant_id = sess["tenant_id"]
            api_key_raw = sess["api_key"]
            t = db.get_tenant(tenant_id)
            plan_name = t["plan"] if t else PLANS.get("free", {})
            html = _render_dashboard(tenant_id, plan_name, api_key_raw)
            return Response(content=html, media_type="text/html")

    # 2. Fall back to query param or header
    api_key_raw = request.headers.get("X-API-Key") or request.query_params.get("api_key")
    if api_key_raw and api_key_raw in API_KEYS:
        info = API_KEYS[api_key_raw]
        tenant_id = info["tenant_id"]
        t = db.get_tenant(tenant_id)
        plan_name = t["plan"] if t else info.get("plan", "free")
        html = _render_dashboard(tenant_id, plan_name, api_key_raw)
        return Response(content=html, media_type="text/html")

    # 3. No valid auth — redirect to login page
    return RedirectResponse(url="/auth/login", status_code=302)


def _error_page(message: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ROMA — Unauthorized</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#0f1117; color:#e5e7eb; display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0 }}
.box {{ text-align:center; padding:40px; background:#161b22; border:1px solid #ef4444; border-radius:12px; max-width:500px }}
h1 {{ font-size:48px; color:#ef4444; margin-bottom:8px }}
p {{ color:#9ca3af; font-size:16px }}
code {{ background:#1f2937; padding:2px 8px; border-radius:4px; font-size:14px }}
a {{ color:#3b82f6 }}
</style></head>
<body>
<div class="box">
    <h1>401</h1>
    <p>{message}</p>
    <p style="margin-top:16px"><a href="/dashboard">Try again</a></p>
</div>
</body></html>"""
