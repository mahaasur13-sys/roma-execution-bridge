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

def verify_api_key(x_api_key: str = Header(None)) -> dict:
    """Validate API key and return tenant info: {tenant_id, name}."""
    if not x_api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing X-API-Key header. Request a key at https://roma-execution-bridge-asurdev.zocomputer.io",
        )
    if x_api_key not in API_KEYS:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return API_KEYS[x_api_key]

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
    """Returns (allowed, reason)."""
    tenant_info = API_KEYS.values()
    plan_name = "free"  # default
    for info in API_KEYS.values():
        if info["tenant_id"] == tenant_id:
            plan_name = info.get("plan", "free")
            break

    plan = PLANS.get(plan_name, PLANS.get("free", {}))
    max_jobs = plan.get("max_jobs_per_month", 50)

    if max_jobs == -1:  # unlimited
        return True, ""

    tenant_usage = _get_tenant_usage(tenant_id)
    if tenant_usage["total_jobs"] >= max_jobs:
        return False, f"Plan '{plan_name}' limit reached: {tenant_usage['total_jobs']}/{max_jobs} jobs. Upgrade at https://roma-execution-bridge-asurdev.zocomputer.io"
    return True, ""

# ============================================
# STRIPE (stub if no keys)
# ============================================

STRIPE_ENABLED = bool(os.environ.get("STRIPE_SECRET_KEY"))

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

    # Determine plan
    plan_name = key_info.get("plan", "free")
    plan = PLANS.get(plan_name, PLANS.get("free", {}))
    usage_data = _get_tenant_usage(tenant_id)
    max_jobs = plan.get("max_jobs_per_month", 50)

    return UsageResponse(
        tenant_id=tenant_id,
        plan=plan_name,
        usage=usage_data,
        limits={
            "max_jobs_per_month": max_jobs,
            "max_jobs_per_month_display": "unlimited" if max_jobs == -1 else str(max_jobs),
        },
    )


@app.post("/billing/create-checkout-session", dependencies=[Depends(verify_api_key)])
async def create_checkout_session(body: CheckoutRequest, key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]
    plan_name = body.plan

    if not STRIPE_ENABLED:
        return {
            "status": "billing_disabled",
            "message": (
                "Stripe is not configured. To enable billing:\n"
                "1. Add STRIPE_SECRET_KEY to Zo Secrets: https://asurdev.zo.computer/?t=settings&s=advanced\n"
                "2. Add STRIPE_PUBLISHABLE_KEY for frontend checkout\n"
                "3. Restart the ROMA service\n\n"
                f"Selected plan: {plan_name} (${PLANS.get(plan_name, {}).get('price_monthly', 0)}/month)"
            ),
            "plan": plan_name,
            "tenant_id": tenant_id,
        }

    # Real Stripe checkout
    try:
        plan = PLANS.get(plan_name, PLANS["pro"])
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"ROMA {plan['name']} Plan"},
                    "unit_amount": plan["price_monthly"] * 100,
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }],
            mode="subscription",
            success_url="https://roma-execution-bridge-asurdev.zocomputer.io/success",
            cancel_url="https://roma-execution-bridge-asurdev.zocomputer.io/cancel",
            metadata={"tenant_id": tenant_id},
        )
        return CheckoutResponse(url=session.url, plan=plan_name, mode="subscription")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")


# ============================================
# ENTRY POINT
# ============================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8899)
