"""
ROMA Execution Bridge – FastAPI + Pydantic v2
Multi-tenant execution platform with API-Key auth and tenant isolation.
"""

import json
import logging
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
# API KEY AUTH + MULTI-TENANCY
# ============================================

API_KEYS_FILE = Path(__file__).parent / "config" / "api_keys.json"

def _load_api_keys() -> dict[str, dict]:
    with open(API_KEYS_FILE) as f:
        return json.load(f)

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

roma_jobs_total = Counter(
    "roma_jobs_total",
    "Total number of submitted jobs",
    ["tenant_id"],
)
roma_jobs_active = Gauge(
    "roma_jobs_active",
    "Currently active jobs",
    ["tenant_id"],
)
roma_queue_depth = Gauge(
    "roma_queue_depth",
    "Current queue depth",
    ["tenant_id"],
)
roma_requests_total = Counter(
    "roma_requests_total",
    "Total HTTP requests",
    ["endpoint", "method", "status"],
)
roma_request_duration = Histogram(
    "roma_request_duration_seconds",
    "Request duration in seconds",
    ["endpoint", "method"],
)

# ============================================
# MIDDLEWARE — structured logging + metrics
# ============================================

@app.middleware("http")
async def tracking_middleware(request: Request, call_next) -> Response:
    start = time.monotonic()
    api_key_raw = request.headers.get("X-API-Key")
    api_key_masked = _mask_key(api_key_raw)

    # Resolve tenant_id early for logging (may be None for public endpoints)
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

jobs: dict[str, dict] = {}             # job_id → job (includes tenant_id)
queue_depth: int = 0


# ============================================
# ENDPOINTS (protected, tenant-isolated)
# ============================================

@app.post("/submit", response_model=RomaTaskResponse, status_code=202, dependencies=[Depends(verify_api_key)])
async def submit_task(payload: RomaTaskInput, request: Request, key_info: dict = Depends(verify_api_key)):
    global queue_depth
    tenant_id = key_info["tenant_id"]

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

        # Count active jobs per tenant
        tenant_jobs = [j for j in jobs.values() if j.get("tenant_id") == tenant_id]
        roma_jobs_active.labels(tenant_id=tenant_id).set(len(tenant_jobs))

        return RomaTaskResponse(
            status="queued",
            job_id=job_id,
            tenant_id=tenant_id,
            roma_dispatch={
                "protocol": "rom",
                "target": job["rom"],
            },
            dag=["validate", "dispatch", "execute", "commit"],
            estimated_resources={
                "cpu_cores": 2,
                "memory_mb": 512,
                "gpu": 0,
            },
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

    # Tenant isolation: return 404 if job belongs to another tenant
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

    return {
        "status": "cancelled",
        "job_id": job_id,
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "queue_depth": queue_depth,
        "jobs": len(jobs),
    }


# ============================================
# /metrics — Prometheus (public)
# ============================================

@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ============================================
# ENTRY POINT
# ============================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8899)


@app.post("/submit/cluster", status_code=202, dependencies=[Depends(verify_api_key)])
async def submit_atom_cluster(payload: dict, key_info: dict = Depends(verify_api_key)):
    """Submit job as ATOMCluster managed execution."""
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
        "atom_cluster": {
            "name": cluster_name,
            "managed": True,
            "nodes": cluster_spec.get("nodes", 1),
        },
    }
    jobs[job_id] = job
    return job


@app.get("/jobs", dependencies=[Depends(verify_api_key)])
async def list_jobs(key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]

    # Filter jobs by tenant
    my_jobs = [j for j in jobs.values() if j.get("tenant_id") == tenant_id]

    return {
        "rom_version": "1.0.0",
        "tenant_id": tenant_id,
        "queue": len(my_jobs),
        "jobs": my_jobs[-10:],
        "execution_modes": ["k8s_job", "k8s_persistent", "atom_cluster", "batch"],
    }
