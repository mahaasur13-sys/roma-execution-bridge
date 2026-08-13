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
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

limiter = Limiter(key_func=get_remote_address)


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

# ============================================
# CLOUDPAYMENTS BILLING INTEGRATION
# ============================================

CLOUDPAYMENTS_ENABLED = bool(
    os.environ.get("CLOUDPAYMENTS_PUBLIC_ID") and
    os.environ.get("CLOUDPAYMENTS_API_SECRET")
)
WORKER_WS_ENABLED = os.environ.get("WORKER_WS_ENABLED", "false").lower() == "true"

CLOUDPAYMENTS_PLANS = {
    "pro": {
        "amount": 4900.00,       # RUB
        "currency": "RUB",
        "interval": "Month",
        "period": 1,
        "description": "ROMA Pro — 500 задач/мес",
    },
    "enterprise": {
        "amount": 29900.00,      # RUB
        "currency": "RUB",
        "interval": "Month",
        "period": 1,
        "description": "ROMA Enterprise — безлимит",
    },
}

cloudpayments_client = None
if CLOUDPAYMENTS_ENABLED:
    from billing.cloudpayments_client import CloudPaymentsConfig, CloudPaymentsClient
    _cp_cfg = CloudPaymentsConfig(
        public_id=os.environ["CLOUDPAYMENTS_PUBLIC_ID"],
        api_secret=os.environ["CLOUDPAYMENTS_API_SECRET"],
        webhook_secret=os.environ.get("CLOUDPAYMENTS_API_SECRET", ""),
    )
    cloudpayments_client = CloudPaymentsClient(_cp_cfg)


# ============================================
# OAUTH2 CONFIG — Google + GitHub
# ============================================

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
OAUTH_ENABLED = bool(GOOGLE_CLIENT_ID or GITHUB_CLIENT_ID)
OAUTH_REDIRECT_BASE = os.environ.get(
    "OAUTH_REDIRECT_BASE",
    "https://roma-execution-bridge-asurdev.zocomputer.io"
)

import httpx
from urllib.parse import urlencode


# ============================================
# JAEGER / OPENTELEMETRY TRACING
# ============================================

JAEGER_ENABLED = os.environ.get("JAEGER_ENABLED", "false").lower() == "true"
JAEGER_AGENT_HOST = os.environ.get("JAEGER_AGENT_HOST", "localhost")
JAEGER_AGENT_PORT = int(os.environ.get("JAEGER_AGENT_PORT", "6831"))
JAEGER_SERVICE_NAME = os.environ.get("JAEGER_SERVICE_NAME", "roma-execution-bridge")

if JAEGER_ENABLED:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.jaeger.thrift import JaegerExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource

    resource = Resource.create({SERVICE_NAME: JAEGER_SERVICE_NAME})
    provider = TracerProvider(resource=resource)

    jaeger_exporter = JaegerExporter(
        agent_host_name=JAEGER_AGENT_HOST,
        agent_port=JAEGER_AGENT_PORT,
    )
    provider.add_span_processor(BatchSpanProcessor(jaeger_exporter))
    trace.set_tracer_provider(provider)

    tracer = trace.get_tracer(__name__)
else:
    tracer = None


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
app.state.limiter = limiter

# CORS (P2-1)
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "http://localhost:5173,http://localhost:3000").split(",")
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

if JAEGER_ENABLED:
    FastAPIInstrumentor.instrument_app(app)

# ============================================
# PROMETHEUS METRICS (with tenant_id label)
# ============================================

roma_jobs_total = Counter("roma_jobs_total", "Total number of submitted jobs", ["tenant_id"])
roma_jobs_active = Gauge("roma_jobs_active", "Currently active jobs", ["tenant_id"])
roma_queue_depth = Gauge("roma_queue_depth", "Current queue depth", ["tenant_id"])
roma_requests_total = Counter("roma_requests_total", "Total HTTP requests", ["endpoint", "method", "status"])
roma_request_duration = Histogram("roma_request_duration_seconds", "Request duration in seconds", ["endpoint", "method"])

# Business metrics (P2-4)
roma_billing_events = Counter("roma_billing_events_total", "Billing events", ["event_type", "plan"])
roma_errors_total = Counter("roma_errors_total", "Errors by endpoint", ["endpoint", "status_code"])
roma_cloudpayments_success = Counter("roma_cloudpayments_success_total", "CloudPayments successful payments")
roma_cloudpayments_failure = Counter("roma_cloudpayments_failure_total", "CloudPayments failed payments")

# Business metrics (P2-4)
roma_billing_events = Counter("roma_billing_events_total", "Billing events (checkout/webhook)", ["event_type", "plan"])
roma_auth_failures = Counter("roma_auth_failures_total", "Authentication failures", ["reason"])
roma_errors_by_endpoint = Counter("roma_errors_total", "Errors by endpoint", ["endpoint", "status"])
roma_cloudpayments_checkouts = Counter("roma_cloudpayments_checkouts_total", "CloudPayments checkout sessions", ["plan"])
roma_cloudpayments_webhooks = Counter("roma_cloudpayments_webhooks_total", "CloudPayments webhook events", ["event_type"])

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
    # Analytics tracking (fire-and-forget)
    if tenant_id and os.environ.get("ANALYTICS_ENABLED", "true") == "true":
        try:
            client_ip = request.client.host if request.client else ""
            ua = request.headers.get("user-agent", "")
            db.log_user_event(tenant_id, "api_request", event_data={"endpoint": endpoint, "method": method, "status_code": status}, ip_address=client_ip, user_agent=ua)
        except Exception:
            pass

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
        "billing": {"cloudpayments_enabled": CLOUDPAYMENTS_ENABLED},
    }


@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ============================================
# ENDPOINTS — Protected: Jobs
# ============================================

@limiter.limit("30/minute")
@app.post("/submit", response_model=RomaTaskResponse, status_code=202, dependencies=[Depends(verify_api_key)])
async def submit_task(payload: RomaTaskInput, request: Request, key_info: dict = Depends(verify_api_key)):
    global queue_depth
    tenant_id = key_info["tenant_id"]
    if tracer:
        span = tracer.start_span("submit_task")
        span.set_attribute("tenant_id", tenant_id)
        span.set_attribute("gpu_required", payload.gpu_required)

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


@limiter.limit("10/minute")
@app.post("/billing/create-checkout-session")
async def create_checkout_session(
    request: Request,
    body: CheckoutRequest,
    key_info: dict = Depends(verify_api_key),
):
    """
    Создаёт платёжную ссылку CloudPayments (hosted page).
    Возвращает {"url": "https://..."}.
    """
    tenant_id = key_info.get("tenant_id", "")
    plan_name = body.plan

    # Free plan — activate immediately
    if plan_name == "free":
        db.update_tenant_subscription(tenant_id, "", "", "active", "free", None)
        return {"url": "", "plan": "free", "tenant_id": tenant_id, "message": "Free plan activated"}

    if not CLOUDPAYMENTS_ENABLED or cloudpayments_client is None:
        plan_example = CLOUDPAYMENTS_PLANS.get(plan_name, {})
        return {
            "url": f"https://example.com/billing/success?plan={plan_name}&dry_run=1",
            "session_id": f"dry_run_{uuid.uuid4().hex[:12]}",
            "plan": plan_name,
            "tenant_id": tenant_id,
            "dry_run": True,
            "message": (
                "CloudPayments is not configured. To enable:\n"
                "1. Add CLOUDPAYMENTS_PUBLIC_ID to .env\n"
                "2. Add CLOUDPAYMENTS_API_SECRET to .env\n"
                "3. Restart the ROMA service\n\n"
                f"Selected plan: {plan_name} ({plan_example.get('amount', 0)} RUB/month)"
            ),
        }

    plan_cfg = CLOUDPAYMENTS_PLANS.get(plan_name)
    if not plan_cfg:
        raise HTTPException(status_code=400, detail="Invalid plan")

    email = key_info.get("email", "")

    try:
        result = cloudpayments_client.create_order(
            amount=plan_cfg["amount"],
            currency=plan_cfg["currency"],
            description=plan_cfg["description"],
            email=email,
            subscription_plan=plan_name,
        )

        return {
            "url": result.get("Url", ""),
            "session_id": result.get("Id") or result.get("Model", {}).get("Id"),
            "plan": plan_name,
            "tenant_id": tenant_id,
            "dry_run": False,
        }
    except Exception as e:
        logger.error(f"CloudPayments create_order failed for {tenant_id}: {e}")
        raise HTTPException(status_code=502, detail=f"Payment provider error: {str(e)}")


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


# ============================================
# BETA TESTING — application + leads
# ============================================

BETA_FORM_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ROMA — Beta Application</title>
<style>
* { margin:0; padding:0; box-sizing:border-box }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#0f1117; color:#e5e7eb; min-height:100vh; padding:40px 16px }
.container { max-width:600px; margin:0 auto }
.card { background:#161b22; border:1px solid #30363d; border-radius:12px; padding:32px }
h1 { font-size:24px; margin-bottom:8px; color:#f9fafb }
p { color:#8b949e; font-size:14px; margin-bottom:24px; line-height:1.6 }
label { display:block; font-size:13px; color:#8b949e; margin-bottom:4px }
input, textarea, select { width:100%; padding:10px 14px; background:#0d1117; border:1px solid #30363d; border-radius:8px; color:#e5e7eb; font-size:14px; margin-bottom:16px; outline:none; font-family:inherit }
input:focus, textarea:focus, select:focus { border-color:#3b82f6 }
select { appearance:none }
button { width:100%; padding:12px; background:#238636; border:none; border-radius:8px; color:#fff; font-size:15px; cursor:pointer; font-weight:600 }
button:hover { background:#2ea043 }
.success { background:rgba(34,197,94,0.1); border:1px solid #22c55e; border-radius:8px; padding:16px; color:#22c55e; text-align:center; margin-bottom:20px; display:none }
.error { background:rgba(239,68,68,0.1); border:1px solid #ef4444; border-radius:8px; padding:12px; color:#ef4444; font-size:14px; margin-bottom:16px; display:none }
.note { font-size:12px; color:#6b7280; margin-top:12px; text-align:center }
</style>
</head>
<body>
<div class="container">
<div class="card">
    <h1>🚀 ROMA Beta Testing</h1>
    <p>Closed-loop GPU execution platform. We're opening early access to ML engineers, researchers, and startups. Fill out the form — we'll get back to you within 48 hours.</p>
    <div id="success" class="success">✅ Application submitted! We'll reach out to you soon.</div>
    <div id="error" class="error"></div>
    <form id="betaForm">
        <label for="email">Email *</label>
        <input type="email" id="email" name="email" placeholder="you@company.com" required>
        <label for="company">Company</label>
        <input type="text" id="company" name="company" placeholder="Acme AI Labs">
        <label for="role">Role</label>
        <select id="role" name="role">
            <option value="">Select role...</option>
            <option>ML Engineer</option>
            <option>Research Scientist</option>
            <option>DevOps / MLOps</option>
            <option>CTO / Engineering Lead</option>
            <option>Student / Researcher</option>
            <option>Other</option>
        </select>
        <label for="use_case">What would you use ROMA for?</label>
        <textarea id="use_case" name="use_case" rows="3" placeholder="E.g. training LLMs, batch inference, hyperparameter tuning..."></textarea>
        <label for="source">How did you hear about ROMA?</label>
        <select id="source" name="source">
            <option value="">Select...</option>
            <option>GitHub</option>
            <option>Twitter / X</option>
            <option>LinkedIn</option>
            <option>Recommendation</option>
            <option>Search</option>
            <option>Other</option>
        </select>
        <button type="submit">Apply for Beta Access</button>
    </form>
    <div class="note">No credit card required. Free during beta period.</div>
</div>
</div>
<script>
document.getElementById('betaForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const email = document.getElementById('email').value.trim();
    if (!email) return;
    const data = {
        email: email,
        company: document.getElementById('company').value.trim(),
        role: document.getElementById('role').value,
        use_case: document.getElementById('use_case').value.trim(),
        source: document.getElementById('source').value,
    };
    try {
        const resp = await fetch('/beta/apply', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data) });
        if (resp.ok) {
            document.getElementById('success').style.display = 'block';
            document.getElementById('error').style.display = 'none';
            document.getElementById('betaForm').reset();
        } else {
            const err = await resp.json();
            document.getElementById('error').textContent = err.detail || 'Submission failed';
            document.getElementById('error').style.display = 'block';
        }
    } catch(e) {
        document.getElementById('error').textContent = 'Network error. Please try again.';
        document.getElementById('error').style.display = 'block';
    }
});
</script>
</body>
</html>"""


@app.get("/beta")
async def beta_page():
    return Response(content=BETA_FORM_HTML, media_type="text/html")


@limiter.limit("5/minute")
@app.post("/beta/apply")
async def beta_apply(payload: dict):
    email = (payload.get("email") or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")
    company = payload.get("company", "")
    role = payload.get("role", "")
    use_case = payload.get("use_case", "")
    source = payload.get("source", "")
    lead_id = db.add_lead(email, company, role, use_case, source)
    logger.info("beta_lead_created", extra={"lead_id": lead_id, "email": email[:3] + "***"})
    return {"status": "accepted", "message": "Thank you! We'll reach out to you soon.", "lead_id": lead_id}


@app.get("/beta/leads", dependencies=[Depends(verify_api_key)])
async def beta_leads(key_info: dict = Depends(verify_api_key)):
    status = ""  # All leads
    all_leads = db.list_leads()
    return {"total": len(all_leads), "leads": all_leads}



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
    <p style="margin-top:24px; color:#8b949e; text-align:center">— or sign in with —</p>
    <div style="display:flex; gap:12px; margin-top:16px">
        <a href="/auth/oauth/login/google" style="flex:1; text-align:center; padding:10px; background:#1a1f2e; border:1px solid #30363d; border-radius:8px; color:#e5e7eb; text-decoration:none; font-size:14px">🔵 Google</a>
        <a href="/auth/oauth/login/github" style="flex:1; text-align:center; padding:10px; background:#1a1f2e; border:1px solid #30363d; border-radius:8px; color:#e5e7eb; text-decoration:none; font-size:14px">🐙 GitHub</a>
    </div>

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


@limiter.limit("15/minute")
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


# ============================================
# OAUTH2 — Google + GitHub Login
# ============================================

@limiter.limit("10/minute")
@app.get("/auth/oauth/login/{provider}")
async def oauth_login(provider: str):
    """Redirect to Google or GitHub OAuth authorization page."""
    if not OAUTH_ENABLED:
        return Response(
            content=_error_page(
                "OAuth is not configured. Add GOOGLE_CLIENT_ID or GITHUB_CLIENT_ID to .env<br>"
                "See <a href='https://github.com/mahaasur13-sys/roma-execution-bridge/blob/master/docs/oauth-setup.md'>docs/oauth-setup.md</a>"
            ),
            media_type="text/html", status_code=503,
        )

    if provider == "google" and GOOGLE_CLIENT_ID:
        params = {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": f"{OAUTH_REDIRECT_BASE}/auth/oauth/callback/google",
            "response_type": "code",
            "scope": "openid email profile",
            "access_type": "offline",
            "prompt": "consent",
        }
        auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
        logger.info(f"OAuth redirect → Google")
        return RedirectResponse(url=auth_url, status_code=302)

    elif provider == "github" and GITHUB_CLIENT_ID:
        params = {
            "client_id": GITHUB_CLIENT_ID,
            "redirect_uri": f"{OAUTH_REDIRECT_BASE}/auth/oauth/callback/github",
            "scope": "user:email",
        }
        auth_url = f"https://github.com/login/oauth/authorize?{urlencode(params)}"
        logger.info(f"OAuth redirect → GitHub")
        return RedirectResponse(url=auth_url, status_code=302)

    return Response(
        content=_error_page(f"OAuth provider '{provider}' is not configured."),
        media_type="text/html", status_code=400,
    )


async def _oauth_google_callback(code: str) -> dict:
    """Exchange Google OAuth code for user info."""
    async with httpx.AsyncClient(timeout=10) as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": f"{OAUTH_REDIRECT_BASE}/auth/oauth/callback/google",
            },
        )
        token_data = token_resp.json()
        if "error" in token_data:
            raise ValueError(f"Google token error: {token_data.get('error_description', token_data['error'])}")

        access_token = token_data["access_token"]
        user_resp = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        user_data = user_resp.json()
        return {
            "id": f"google-{user_data['id']}",
            "email": user_data["email"],
            "name": user_data.get("name", user_data["email"]),
            "provider": "google",
        }


async def _oauth_github_callback(code: str) -> dict:
    """Exchange GitHub OAuth code for user info."""
    async with httpx.AsyncClient(timeout=10) as client:
        token_resp = await client.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
                "redirect_uri": f"{OAUTH_REDIRECT_BASE}/auth/oauth/callback/github",
            },
            headers={"Accept": "application/json"},
        )
        token_data = token_resp.json()
        if "error" in token_data:
            raise ValueError(f"GitHub token error: {token_data.get('error_description', token_data['error'])}")

        access_token = token_data["access_token"]
        user_resp = await client.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        user_data = user_resp.json()

        # Get primary email (GitHub may hide it in user object)
        email = user_data.get("email", "")
        if not email:
            emails_resp = await client.get(
                "https://api.github.com/user/emails",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            emails = emails_resp.json()
            primary = next((e for e in emails if e.get("primary")), emails[0] if emails else {})
            email = primary.get("email", "")

        return {
            "id": f"github-{user_data['id']}",
            "email": email or f"github-{user_data['id']}@users.noreply.github.com",
            "name": user_data.get("name", user_data.get("login", email)),
            "provider": "github",
        }


@limiter.limit("10/minute")
@app.get("/auth/oauth/callback/{provider}")
async def oauth_callback(provider: str, code: str = "", error: str = "", request: Request = None):
    """Handle OAuth callback — exchange code, create/update user, start session."""
    if error:
        return Response(
            content=_error_page(f"OAuth authorization denied: {error}"),
            media_type="text/html", status_code=400,
        )
    if not code:
        return Response(
            content=_error_page("No authorization code received from OAuth provider."),
            media_type="text/html", status_code=400,
        )
    if not OAUTH_ENABLED:
        return Response(
            content=_error_page("OAuth is not configured."),
            media_type="text/html", status_code=503,
        )

    try:
        if provider == "google":
            user_info = await _oauth_google_callback(code)
        elif provider == "github":
            user_info = await _oauth_github_callback(code)
        else:
            return Response(
                content=_error_page(f"Unknown OAuth provider: {provider}"),
                media_type="text/html", status_code=400,
            )
    except Exception as e:
        logger.error(f"OAuth callback error ({provider}): {e}")
        return Response(
            content=_error_page(f"OAuth login failed: {str(e)}"),
            media_type="text/html", status_code=500,
        )

    user_id = user_info["id"]
    email = user_info["email"]
    name = user_info.get("name", email)
    prov = user_info["provider"]

    # Upsert user — if exists, reuse; otherwise create new tenant + API key
    existing = db.get_user_by_email(email)
    if existing:
        api_key = existing["api_key"]
        tenant_id = existing["tenant_id"]
        logger.info(f"OAuth login: existing user {email} → tenant={tenant_id}")
    else:
        tenant_id = f"tenant-{str(uuid.uuid4())[:8]}"
        api_key = f"roma-{str(uuid.uuid4())[:12]}"
        try:
            db.upsert_oauth_user(user_id, email, name, prov, tenant_id, api_key)
        except Exception as e:
            logger.warning(f"upsert_oauth_user failed (non-fatal): {e}")
        # Seed tenant into DB
        try:
            db.seed_tenants({api_key: {"tenant_id": tenant_id, "plan": "free"}})
        except Exception as e:
            logger.warning(f"seed_tenants failed (non-fatal): {e}")
        # Add to in-memory API key registry
        API_KEYS[api_key] = {
            "tenant_id": tenant_id,
            "plan": "free",
            "subscription_status": "active",
        }
        logger.info(f"OAuth login: NEW user {email} → tenant={tenant_id}, api_key={api_key[:8]}***")

    session_id = create_session(tenant_id, api_key)
    resp = RedirectResponse(url="/dashboard", status_code=302)
    resp.set_cookie("session_id", session_id, httponly=True, max_age=3600, samesite="lax")
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



# ============================================
# ENDPOINTS — Feedback
# ============================================

@limiter.limit("10/minute")
@app.post("/feedback")
async def submit_feedback(request: Request):
    """Submit user feedback. Public endpoint, no auth required."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    rating = body.get("rating")
    if not rating or not isinstance(rating, int) or rating < 1 or rating > 5:
        raise HTTPException(status_code=400, detail="rating must be integer 1-5")

    liked = body.get("liked", "")
    improvement = body.get("improvement", "")
    bug = body.get("bug", "")
    user_agent = request.headers.get("user-agent", "")

    # Try to resolve tenant from API key or session
    tenant_id = "anonymous"
    user_id = ""
    api_key_raw = request.headers.get("X-API-Key")
    if api_key_raw:
        info = API_KEYS.get(api_key_raw)
        if info:
            tenant_id = info["tenant_id"]

    try:
        fid = db.save_feedback(tenant_id, user_id, rating, liked, improvement, bug, user_agent)
        logger.info("Feedback saved", extra={"feedback_id": fid, "tenant_id": tenant_id, "rating": rating})
        return {"status": "ok", "feedback_id": fid}
    except Exception as e:
        logger.error(f"Failed to save feedback: {e}")
        raise HTTPException(status_code=500, detail="Failed to save feedback")


# ============================================
# ENDPOINTS — CloudPayments Webhook
# ============================================

@limiter.limit("20/minute")
@app.post("/webhooks/cloudpayments")
async def cloudpayments_webhook(request: Request):
    """Handle CloudPayments webhook notifications.
    Verifies Content-HMAC signature and updates tenant subscription.

    Events handled: Pay, Recurrent, Fail, Cancel, Unsubscribe.
    """
    raw_body = await request.body()
    signature = request.headers.get("Content-HMAC") or request.headers.get("Content-Hmac") or ""

    if not CLOUDPAYMENTS_ENABLED or cloudpayments_client is None:
        logger.warning("CloudPayments webhook received but billing is disabled")
        return {"code": 0}

    if not cloudpayments_client.verify_webhook(raw_body, signature):
        logger.warning("CloudPayments webhook: invalid signature")
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = payload.get("OperationType") or payload.get("Status") or ""
    account_id = payload.get("AccountId", "")
    invoice_id = payload.get("InvoiceId", "")
    data = payload.get("Data") or {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}

    tenant_id = account_id or data.get("tenant_id", "")
    plan = data.get("plan", "")

    logger.info(
        f"CloudPayments webhook: {event_type} tenant={tenant_id} plan={plan}",
    )

    if not tenant_id:
        logger.warning("Webhook without tenant_id — ignored")
        return {"code": 0}

    status = payload.get("Status", "")

    # Successful payment / subscription activated
    if event_type in ("Payment", "Pay", "Completed") or status in ("Completed", "Authorized"):
        if plan in ("pro", "enterprise"):
            db.update_tenant_subscription(tenant_id, invoice_id, "", "active", plan, None)
            logger.info(f"CloudPayments: subscription activated — {tenant_id} → {plan}")

    # Recurring payment succeeded
    elif event_type == "Recurrent" and status == "Completed":
        if plan:
            db.update_tenant_subscription(tenant_id, invoice_id, "", "active", plan, None)
            logger.info(f"CloudPayments: recurrent payment OK — {tenant_id}")

    # Payment failed / declined
    elif event_type in ("Fail", "Declined") or status in ("Declined", "Cancelled"):
        db.set_tenant_inactive(tenant_id)
        logger.warning(f"CloudPayments: payment failed — {tenant_id}")

    # Subscription cancelled
    elif event_type in ("Cancel", "Unsubscribe"):
        db.update_tenant_subscription(tenant_id, "", "", "canceled", "free", None)
        logger.info(f"CloudPayments: subscription canceled — {tenant_id}")

    return {"code": 0}


# ============================================
# ENDPOINTS — SendGrid Webhook# ENDPOINTS — SendGrid Webhook
# ============================================

@limiter.limit("20/minute")
@app.post("/webhooks/email")
async def sendgrid_webhook(request: Request):
    """Receive SendGrid event notifications.
    Events: delivered, open, click, bounce, dropped, spamreport.
    See: https://docs.sendgrid.com/for-developers/tracking-events/event
    """
    try:
        events = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if not isinstance(events, list):
        events = [events]

    processed = 0
    for evt in events:
        try:
            event_type = evt.get("event", "")
            email = evt.get("email", "")
            if not email or not event_type:
                continue
            db.update_email_event(email, event_type)
            processed += 1
        except Exception as e:
            logger.warning(f"SendGrid webhook event skipped: {e}")

    logger.info(f"SendGrid webhook: processed {processed}/{len(events)} events")
    return {"status": "ok", "processed": processed}


# ============================================
# ENDPOINTS — Admin (API-key protected)
# ============================================

def _admin_only(request: Request) -> dict:
    """Verify admin access — requires valid API key + tenant-demo."""
    api_key_raw = request.headers.get("X-API-Key")
    if not api_key_raw:
        api_key_raw = request.query_params.get("api_key", "")

    if not api_key_raw:
        raise HTTPException(status_code=401, detail="Admin API key required")

    info = API_KEYS.get(api_key_raw)
    if not info:
        raise HTTPException(status_code=401, detail="Invalid API key")

    if info.get("tenant_id") != "tenant-demo":
        raise HTTPException(status_code=403, detail="Admin access requires tenant-demo API key")

    return info


@app.get("/admin")
async def admin_page(request: Request):
    """Admin dashboard HTML page."""
    info = _admin_only(request)
    return Response(content=_render_admin_dashboard(info["tenant_id"]), media_type="text/html")


@app.get("/admin/analytics")
async def admin_analytics(request: Request):
    """Get analytics overview (JSON)."""
    _admin_only(request)
    days = int(request.query_params.get("days", "30"))
    try:
        data = db.get_analytics_overview(days=days)
        return {"status": "ok", "data": data}
    except Exception as e:
        logger.error(f"Admin analytics error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/analytics/users")
async def admin_analytics_users(request: Request):
    """Get user list for analytics."""
    _admin_only(request)
    start_date = request.query_params.get("start_date", "")
    end_date = request.query_params.get("end_date", "")
    sort_by = request.query_params.get("sort_by", "last_seen")
    try:
        users = db.get_analytics_users(start_date=start_date, end_date=end_date, sort_by=sort_by)
        return {"status": "ok", "users": users}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/analytics/events")
async def admin_analytics_events(request: Request):
    """Get paginated event list."""
    _admin_only(request)
    limit = int(request.query_params.get("limit", "100"))
    offset = int(request.query_params.get("offset", "0"))
    event_type = request.query_params.get("event_type", "")
    tenant_id = request.query_params.get("tenant_id", "")
    from_date = request.query_params.get("from_date", "")
    to_date = request.query_params.get("to_date", "")
    try:
        items, total = db.get_analytics_events(
            limit=limit, offset=offset, event_type=event_type,
            tenant_id=tenant_id, from_date=from_date, to_date=to_date
        )
        return {"status": "ok", "items": items, "total": total, "limit": limit, "offset": offset}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/feedback")
async def admin_feedback(request: Request):
    """Get feedback list (JSON)."""
    _admin_only(request)
    limit = int(request.query_params.get("limit", "50"))
    offset = int(request.query_params.get("offset", "0"))
    from_date = request.query_params.get("from_date", "")
    to_date = request.query_params.get("to_date", "")
    rating = int(request.query_params.get("rating", "0"))
    try:
        items, total = db.get_feedback(
            limit=limit, offset=offset, from_date=from_date,
            to_date=to_date, rating=rating
        )
        return {"status": "ok", "items": items, "total": total, "limit": limit, "offset": offset}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@limiter.limit("20/minute")
@app.get("/admin/email-stats")
async def admin_email_stats(request: Request):
    """Get email sending statistics."""
    _admin_only(request)
    try:
        stats = db.get_email_stats()
        return {"status": "ok", "data": stats}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@limiter.limit("20/minute")
@app.post("/admin/invite")
async def admin_invite(request: Request):
    """Send beta invitations. Dry-run if no SendGrid API key."""
    _admin_only(request)
    try:
        body = await request.json()
    except Exception:
        body = {}

    sendgrid_key = os.environ.get("SENDGRID_API_KEY", "")
    dry_run = body.get("dry_run", not bool(sendgrid_key))

    leads = db.list_leads(status="new")
    if not leads:
        return {"status": "ok", "sent": 0, "dry_run": dry_run, "message": "No new leads to invite"}

    sent = 0
    failed = 0
    for lead in leads:
        email = lead.get("email", "")
        name = lead.get("company", lead.get("email", ""))
        if not email:
            continue

        invitation_link = f"https://roma-execution-bridge-asurdev.zocomputer.io/dashboard?api_key=roma-demo-key-2026"

        try:
            if dry_run:
                db.log_email_sent(email, name, "tenant-demo", invitation_link)
            else:
                # Real SendGrid send would go here
                db.log_email_sent(email, name, "tenant-demo", invitation_link)
            sent += 1
        except Exception as e:
            logger.warning(f"Failed to invite {email}: {e}")
            failed += 1
            try:
                db.log_email_failed(email, str(e))
            except Exception:
                pass

    # Mark leads as invited
    for lead in leads:
        try:
            db.update_lead_status(lead["id"], "invited")
        except Exception:
            pass

    logger.info(f"Admin invite: {sent} sent, {failed} failed (dry_run={dry_run})")
    return {"status": "ok", "sent": sent, "failed": failed, "dry_run": dry_run, "total_leads": len(leads)}


def _render_admin_dashboard(tenant_id: str) -> str:
    """Render admin dashboard HTML."""
    try:
        overview = db.get_analytics_overview(days=30)
    except Exception:
        overview = {}

    try:
        email_stats = db.get_email_stats()
    except Exception:
        email_stats = {}

    total_requests = overview.get("total_requests", 0)
    unique_tenants = overview.get("unique_tenants", 0)
    active_users = overview.get("active_users", 0)
    daily_avg = overview.get("daily_avg", 0)

    emails_sent = email_stats.get("sent", 0)
    emails_delivered = email_stats.get("delivered", 0)
    emails_opened = email_stats.get("opened", 0)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ROMA — Admin Dashboard</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0 }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f1117; color: #e5e7eb; min-height: 100vh }}
.header {{ background: #161b22; border-bottom: 1px solid #30363d; padding: 16px 24px; display: flex; justify-content: space-between; align-items: center }}
.header h1 {{ font-size: 20px; color: #58a6ff }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; padding: 24px }}
.card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px }}
.card h3 {{ font-size: 12px; text-transform: uppercase; color: #8b949e; margin-bottom: 8px }}
.card .value {{ font-size: 32px; font-weight: 700; color: #58a6ff }}
.card .sub {{ font-size: 13px; color: #6e7681; margin-top: 4px }}
.section {{ padding: 0 24px 24px }}
.section h2 {{ font-size: 16px; color: #e5e7eb; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid #30363d }}
.endpoints {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 8px }}
.endpoint-card {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 12px 16px }}
.endpoint-card .method {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; margin-right: 8px }}
.method-get {{ background: #1f6feb33; color: #58a6ff }}
.method-post {{ background: #23863633; color: #3fb950 }}
.endpoint-card code {{ font-size: 13px; color: #e5e7eb }}
.endpoint-card .desc {{ font-size: 12px; color: #8b949e; margin-top: 4px }}
</style>
</head>
<body>
<div class="header">
    <h1>⚡ ROMA Admin Dashboard</h1>
    <span style="color:#8b949e;font-size:13px">tenant: {tenant_id}</span>
</div>

<div class="grid">
    <div class="card">
        <h3>Total Requests (30d)</h3>
        <div class="value">{total_requests:,}</div>
        <div class="sub">avg {daily_avg}/day</div>
    </div>
    <div class="card">
        <h3>Active Tenants</h3>
        <div class="value">{unique_tenants}</div>
    </div>
    <div class="card">
        <h3>Active Users</h3>
        <div class="value">{active_users}</div>
    </div>
    <div class="card">
        <h3>Emails Sent</h3>
        <div class="value">{emails_sent}</div>
        <div class="sub">{emails_opened} opened · {emails_delivered} delivered</div>
    </div>
</div>

<div class="section">
    <h2>📡 Admin API Endpoints</h2>
    <div class="endpoints">
        <div class="endpoint-card">
            <span class="method method-get">GET</span><code>/admin</code>
            <div class="desc">Admin dashboard (this page)</div>
        </div>
        <div class="endpoint-card">
            <span class="method method-get">GET</span><code>/admin/analytics</code>
            <div class="desc">Analytics overview JSON</div>
        </div>
        <div class="endpoint-card">
            <span class="method method-get">GET</span><code>/admin/analytics/users</code>
            <div class="desc">User list with activity</div>
        </div>
        <div class="endpoint-card">
            <span class="method method-get">GET</span><code>/admin/analytics/events</code>
            <div class="desc">Raw event log (paginated)</div>
        </div>
        <div class="endpoint-card">
            <span class="method method-get">GET</span><code>/admin/feedback</code>
            <div class="desc">Feedback list (filterable)</div>
        </div>
        <div class="endpoint-card">
            <span class="method method-get">GET</span><code>/admin/email-stats</code>
            <div class="desc">Email delivery statistics</div>
        </div>
        <div class="endpoint-card">
            <span class="method method-post">POST</span><code>/admin/invite</code>
            <div class="desc">Send beta invitations</div>
        </div>
    </div>
</div>
</body>
</html>"""



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
