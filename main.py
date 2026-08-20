"""
ROMA Execution Bridge – FastAPI + Pydantic v2
Multi-tenant execution platform with API-Key auth, tenant isolation, and billing.
"""
# ── Load .env BEFORE all imports ─────────────────────────────────
import os as _os
from pathlib import Path as _Path
_env_path = _Path(__file__).parent / ".env"
if _env_path.exists():
    with open(_env_path, "r") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _val = _line.partition("=")
                _os.environ.setdefault(_key.strip(), _val.strip().strip('"').strip("'"))
    print(f"✅ Loaded {_env_path}", flush=True)


import json
import logging
import os
import ipaddress
import time
import traceback
import uuid
import secrets
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import db_adapter as db

# DecisionOS — Week 1 foundation
from models.decision import DecisionRequest
from cost.gate import EnterpriseDecisionGate
from audit.event_store import write_event, on_job_created

from fastapi import Depends, FastAPI, Header, HTTPException
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
from monitoring.metrics import (
    gpu_seconds_total,
    tokens_total,
    billing_cost_total,
    spend_cap_balance,
    spend_cap_pct,
    spend_cap_blocked_total,
    job_cost,
    track_billing,
    track_spend_cap,
    track_spend_cap_blocked,
)
from alerts import AlertDispatcher, Alert, AlertLevel

# === BILLING SINGLETONS (v2.1.0) ===
from billing.pg_metering import PGMeteringEngine as MeteringEngine
from billing.pg_ledger import PGBillingLedger as BillingLedger

metering_engine = MeteringEngine()
billing_ledger = BillingLedger()
alert_dispatcher = AlertDispatcher()

from saas.email.service import EmailService, EmailProvider as _EmailProvider
email_service = EmailService(
    provider=_EmailProvider[os.environ.get("EMAIL_PROVIDER", "console").upper()] if os.environ.get("EMAIL_PROVIDER", "console").upper() in ("SMTP","SENDGRID","RESEND","CONSOLE") else _EmailProvider.CONSOLE,
    smtp_host=os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com"),
    smtp_port=int(os.environ.get("EMAIL_SMTP_PORT", "587")),
    smtp_user=os.environ.get("EMAIL_SMTP_USER", ""),
    smtp_password=os.environ.get("EMAIL_SMTP_PASSWORD", ""),
    from_email=os.environ.get("FROM_EMAIL", "beta@roma-execution-bridge.io"),
    from_name=os.environ.get("FROM_NAME", "ROMA Platform"),
    sendgrid_api_key=os.environ.get("SENDGRID_API_KEY", ""),
)
_burn_tracker: dict[str, list[tuple[float, float]]] = {}
_http_request_count = 0
_billing_event_count = 0

def _increment_usage(
    tenant_id: str,
    gpu_sec: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    plan_name: str = "free",
    job_id: str | None = None,
):
    """Единая точка списания денег: GPU-sec + token counting."""
    if not tenant_id:
        return 0.0

    total_cost = 0.0

    # GPU cost
    if gpu_sec > 0:
        gpu_rate = 0.00001
        gpu_cost = round(gpu_sec * gpu_rate, 8)
        metering_engine.record(
            event_type="gpu_usage", tenant=tenant_id,
            gpu_seconds=gpu_sec, job_id=job_id or "auto",
        )
        billing_ledger.append(
            tenant_id=tenant_id, entry_type="debit", amount=gpu_cost,
            metadata={"gpu_sec": gpu_sec, "plan": plan_name, "job_id": job_id},
        )
        total_cost += gpu_cost
        gpu_seconds_total.labels(tenant_id=tenant_id, plan=plan_name).inc(gpu_sec)
        billing_cost_total.labels(tenant_id=tenant_id, plan=plan_name, cost_type="gpu").inc(gpu_cost)

    # Token cost
    if input_tokens > 0 or output_tokens > 0:
        in_rate = 0.000001
        out_rate = 0.000002
        token_cost = round(input_tokens * in_rate + output_tokens * out_rate, 8)
        metering_engine.record(
            event_type="token_usage", tenant=tenant_id,
            gpu_seconds=0, job_id=job_id or "auto",
        )
        billing_ledger.append(
            tenant_id=tenant_id, entry_type="debit", amount=token_cost,
            metadata={"input_tokens": input_tokens, "output_tokens": output_tokens, "job_id": job_id},
        )
        total_cost += token_cost
        if input_tokens > 0:
            tokens_total.labels(tenant_id=tenant_id, plan=plan_name, direction="input").inc(input_tokens)
        if output_tokens > 0:
            tokens_total.labels(tenant_id=tenant_id, plan=plan_name, direction="output").inc(output_tokens)
        token_cost_input = input_tokens * in_rate
        token_cost_output = output_tokens * out_rate
        if token_cost_input > 0:
            billing_cost_total.labels(tenant_id=tenant_id, plan=plan_name, cost_type="tokens").inc(token_cost_input)
        if token_cost_output > 0:
            billing_cost_total.labels(tenant_id=tenant_id, plan=plan_name, cost_type="tokens").inc(token_cost_output)

    if total_cost > 0:
        job_cost.labels(tenant_id=tenant_id, plan=plan_name).observe(total_cost)

    # Burn-rate tracking: alert if >$1/hour over recent window
    _burn_tracker.setdefault(tenant_id, []).append((time.time(), total_cost))
    _burn_tracker[tenant_id] = [(t, c) for t, c in _burn_tracker[tenant_id] if time.time() - t < 3600]
    recent_cost = sum(c for t, c in _burn_tracker[tenant_id] if time.time() - t < 600)
    burn_rate_hourly = recent_cost * 6 if recent_cost > 0 else 0
    if burn_rate_hourly > 1.0:
        alert_dispatcher.send(Alert(
            level=AlertLevel.WARNING,
            title="🔥 High GPU Burn Rate",
            body=f"Tenant `{tenant_id}` (plan `{plan_name}`) burn rate: **${burn_rate_hourly:.2f}/hour**\n"
                 f"Last 10 min cost: ${recent_cost:.4f} → projected ${burn_rate_hourly:.2f}/hour\n"
                 f"Threshold: $1.00/hour",
            tags={"tenant_id": tenant_id, "plan": plan_name, "event": "high_burn_rate"},
        ))

    return total_cost


def _check_spend_cap(tenant_id: str, estimated_cost: float, plan_name: str = "free"):
    """Проверка spend-cap ПЕРЕД созданием job."""
    plan = PLANS.get(plan_name, PLANS.get("free", {}))
    cap = plan.get("spend_cap_usd", 0)
    if cap <= 0:
        return True, ""
    balance = billing_ledger.get_tenant_balance(tenant_id) if hasattr(billing_ledger, "get_tenant_balance") else billing_ledger.get_balance(tenant_id) if hasattr(billing_ledger, "get_balance") else 0.0
    projected = balance + estimated_cost
    track_spend_cap(tenant_id, plan_name, balance, cap)
    if projected > cap:
        pct = int(balance / cap * 100) if cap > 0 else 0
        reason = f"Spend cap exceeded: ${balance:.4f}/${cap:.2f} ({pct}%). Job ${estimated_cost:.6f} exceeds cap."
        logger.warning("spend_cap_blocked tenant=%s plan=%s balance=%.4f cap=%.2f", tenant_id, plan_name, balance, cap)
        track_spend_cap_blocked(tenant_id, plan_name)
        alert_dispatcher.send(Alert(
            level=AlertLevel.CRITICAL,
            title="🚫 Spend-Cap Exceeded",
            body=f"Tenant `{tenant_id}` (plan `{plan_name}`) exceeded spend-cap.\n"
                 f"Balance: **${balance:.4f}** / Cap: **${cap:.2f}** ({pct}%)\n"
                 f"Attempted job cost: ${estimated_cost:.6f}\n"
                 f"Projected: ${projected:.6f} → BLOCKED",
            tags={"tenant_id": tenant_id, "plan": plan_name, "event": "spend_cap_exceeded"},
        ))
        return False, reason
    if cap > 0 and balance / cap >= 0.9:
        pct = int(balance / cap * 100) if cap > 0 else 0
        logger.warning("spend_cap_90%% tenant=%s plan=%s balance=%.4f cap=%.2f", tenant_id, plan_name, balance, cap)
        alert_dispatcher.send(Alert(
            level=AlertLevel.WARNING,
            title="⚠️ Spend-Cap 90% Reached",
            body=f"Tenant `{tenant_id}` (plan `{plan_name}`) approaching spend-cap.\n"
                 f"Balance: **${balance:.4f}** / Cap: **${cap:.2f}** ({pct}%)\n"
                 f"Remaining: **${cap - balance:.4f}**",
            tags={"tenant_id": tenant_id, "plan": plan_name, "event": "spend_cap_90"},
        ))
    return True, ""

from pydantic import BaseModel, Field, ConfigDict
from starlette.requests import Request
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, Response, StreamingResponse
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

API_KEYS: dict[str, dict] = {}

db.init_db()
db.seed_tenants(API_KEYS)

def verify_api_key(x_api_key: str = Header(None)) -> dict:
    """Validate API key and return tenant info: {tenant_id, name, tier, api_key}."""
    if not x_api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing X-API-Key header. Request a key at https://roma-execution-bridge-asurdev.zocomputer.io",
        )
    tenant = db.find_tenant_by_key(x_api_key)
    if not tenant:
        raise HTTPException(status_code=401, detail="Invalid API key")
    tenant["api_key"] = x_api_key
    
    # Check email verification for API endpoints (skip for auth/browser-only endpoints — handled by caller)
    verif_status = is_email_verified(x_api_key)
    if not verif_status:
        raise HTTPException(status_code=403, detail="Email not verified. Please verify your email first.")
    return tenant

# ============================================
# PLANS & USAGE — DecisionOS PG-backed
# ============================================

PLANS: dict = {
    "free": {"max_jobs_per_month": 50, "max_gpu_seconds": 0, "spend_cap_usd": 0.00, "overage_rate": 0.0},
    "start": {"max_jobs_per_month": 50, "max_gpu_seconds": 3600, "spend_cap_usd": 5.00, "overage_rate": 0.000005},
    "pro": {"max_jobs_per_month": 150, "max_gpu_seconds": 36000, "spend_cap_usd": 50.00, "overage_rate": 0.000003},
    "enterprise": {"max_jobs_per_month": -1, "max_gpu_seconds": -1, "spend_cap_usd": -1.0, "overage_rate": 0.0},
}

# ============================================
# DECISIONOS — PostgreSQL-backed plans & usage
# ============================================

# Lazy-init gate (needs DB adapter)
_gate: EnterpriseDecisionGate | None = None

def _get_gate() -> EnterpriseDecisionGate:
    global _gate
    if _gate is None:
        _gate = EnterpriseDecisionGate(db_adapter=db)
    return _gate

# ============================================
# STRIPE — real integration with test-mode fallback
# ============================================

# ============================================
# CLOUDPAYMENTS BILLING INTEGRATION
# ============================================

ADMIN_IP_ALLOWLIST = os.environ.get("ADMIN_IP_ALLOWLIST", "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16")

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
    image: Optional[str] = Field(default=None, max_length=300)
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


class ChatMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str = Field(..., min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=10)
    name: Optional[str] = Field(default=None, max_length=80)

# ============================================
# APP
# ============================================

app = FastAPI(
    title="ROMA Execution Platform",
    version="2.1.0",
)
app.state.limiter = limiter

# CORS (P2-1)
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "http://localhost:5173,http://localhost:3000").split(",")
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# DecisionOS Week 2 — v1 API routes
from routes.v1_router import router as v1_router
from router_decisions import router as decisions_router
from router_jobs import router as jobs_router
from crypto_payments.router import router as crypto_router
from crypto_payments.wallets.router import router as wallets_router
from support_chat.router import router as support_router
app.include_router(v1_router)
app.include_router(decisions_router)
app.include_router(jobs_router)
app.include_router(crypto_router)
app.include_router(wallets_router)
app.include_router(support_router)
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

if JAEGER_ENABLED:
    FastAPIInstrumentor.instrument_app(app)

STATIC_INDEX = Path(__file__).parent / "static" / "index.html"

# ============================================
# PROMETHEUS METRICS (with tenant_id label)
# ============================================
@app.get("/", include_in_schema=False)
async def landing_page():
    return FileResponse(STATIC_INDEX, media_type="text/html")



roma_jobs_total = Counter("roma_jobs_total", "Total number of submitted jobs", ["tenant_id"])
roma_jobs_active = Gauge("roma_jobs_active", "Currently active jobs", ["tenant_id"])
roma_queue_depth = Gauge("roma_queue_depth", "Current queue depth", ["tenant_id"])
queue_depth = 0
roma_requests_total = Counter("roma_requests_total", "Total HTTP requests", ["endpoint", "method", "status"])
roma_request_duration = Histogram("roma_request_duration_seconds", "Request duration in seconds", ["endpoint", "method"])

# Business metrics (P2-4)
roma_billing_events = Counter("roma_billing_events_total", "Billing events", ["event_type", "plan"])
roma_errors_total = Counter("roma_errors_total", "Errors by endpoint", ["endpoint", "status_code"])
roma_cloudpayments_success = Counter("roma_cloudpayments_success_total", "CloudPayments successful payments")
roma_cloudpayments_failure = Counter("roma_cloudpayments_failure_total", "CloudPayments failed payments")

# Business metrics (P2-4)
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

    global _http_request_count
    _http_request_count += 1

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
# ENDPOINTS — Public
# ============================================

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
if not DEEPSEEK_API_KEY:
    try:
        DEEPSEEK_API_KEY = (Path(__file__).parent / "config" / "deepseek_key.txt").read_text().strip()
    except Exception:
        pass
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

ROMA_INTERNAL_BASE_URL = "http://127.0.0.1:8900"

DEEPSEEK_CLIENT = None
if AsyncOpenAI and DEEPSEEK_API_KEY:
    DEEPSEEK_CLIENT = AsyncOpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL,
    )

SYSTEM_PROMPT = """Ты — ROMA AI, дружелюбный и честный помощник платформы ROMA Execution Bridge.

Твоя главная задача — просто и понятно объяснять цены и помогать новичкам быстро разобраться, сколько будет стоить работа с GPU и машинным обучением.

## Актуальные цены ROMA

Планы:
- Free — $0 (можно попробовать бесплатно)
- Start — лимит $5
- Pro — лимит $50
- Enterprise — без лимита

Стоимость:
- 1 секунда GPU = $0.00001 (это примерно $0.036 в час)
- 1 миллион токенов на вход = $1
- 1 миллион токенов на выход = $2

## Как отвечать новичкам

- Говори простыми словами, без сложных терминов
- Всегда показывай пример в долларах
- Сравнивай с рынком честно, но подчёркивай удобство ROMA
- Если задача дороже лимита — мягко предложи увеличить план
- Помогай человеку быстро понять выгоду и принять решение
- Не утверждай результат действия, пока tool не вернул его
- Никогда не проси пользователя вставлять секрет в сообщение (X-API-Key уже передан)

## ЖЁСТКОЕ ПРАВИЛО — никаких месячных подписок

- **НИКОГДА** не называй цены $99, $499 или любые другие месячные подписки.
  В ROMA нет ежемесячной платы. Есть только spend-cap (лимит) и стоимость использования.
- Если пользователь спрашивает «какие тарифы?», «сколько стоит?», «какие планы?» —
  отвечай ТОЛЬКО про spend-cap и цены GPU/токенов.
- Не используй слова «подписка», «месячный платёж», «тарифный план».
  Используй: «лимит», «spend-cap», «баланс».
- Пример ПРАВИЛЬНОГО ответа на «какие у вас тарифы?»:
  «У нас нет ежемесячной платы. Вы платите только за использование:
   Free — лимит $0 (попробовать), Start — $5, Pro — $50, Enterprise — без лимита.
   GPU: $0.00001/сек ($0.036/час). Токены: $1 за 1M входа, $2 за 1M выхода.»


## Когда использовать tools

- «сколько я потратил», «какой баланс», «покажи usage» → get_balance
- «какой у меня план», «какие лимиты», «что я могу» → get_balance
- «история платежей», «за что списано» → get_billing_ledger
- «хватит ли денег на задачу» → check_spend_cap
- «запусти задачу», «отправь job» → submit_task
- «покажи задачи», «какие jobs» → list_jobs
- «отмени задачу» → cancel_job
- «какие воркеры» → list_workers
- «статистика за сегодня» → get_daily_stats

Если пользователь пишет «помощь» или «help», не вызывай tools. Ответь:
Привет! Я AI-ассистент ROMA.

Вот что я умею:
• 💰 Биллинг — «сколько я потратил?», «какой у меня план?», «проверь лимит»
• 🚀 Задачи — «запусти задачу train.py», «покажи мои задачи», «отмени abc-123»
• 📊 Статистика — «покажи usage», «статистика за сегодня»
• ⚙️ Воркеры — «какие воркеры свободны?»

Enter — отправить | Shift+Enter — новая строка | Stop — остановить | 🗑 — очистить"""


def _tool_schema(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return {"type": "function", "function": {"name": name, "description": description, "parameters": schema}}


ROMA_TOOLS = [
    _tool_schema("submit_task", "Отправить ML-задачу на выполнение.", {
        "task": {"type": "string"}, "gpu_required": {"type": "boolean"},
        "image": {"type": "string"}, "priority": {"type": "integer", "minimum": 1, "maximum": 10},
    }, ["task"]),
    _tool_schema("get_job_status", "Получить статус задачи.", {"job_id": {"type": "string"}}, ["job_id"]),
    _tool_schema("list_jobs", "Получить задачи текущего tenant.", {"limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
    _tool_schema("cancel_job", "Отменить задачу.", {"job_id": {"type": "string"}}, ["job_id"]),
    _tool_schema("list_workers", "Получить список воркеров.", {}),
    _tool_schema("drain_worker", "Перевести воркер в drain.", {"worker_id": {"type": "string"}}, ["worker_id"]),
    _tool_schema("slurm_status", "Получить статус Slurm-задачи.", {"slurm_job_id": {"type": "string"}}, ["slurm_job_id"]),
    _tool_schema("slurm_cancel", "Отменить Slurm-задачу.", {"slurm_job_id": {"type": "string"}}, ["slurm_job_id"]),
    _tool_schema("get_usage", "Получить использование и лимиты.", {}),
    _tool_schema("create_checkout_session", "Создать checkout-сессию CloudPayments для плана.", {"plan": {"type": "string", "enum": ["free", "pro", "enterprise"]}}, ["plan"]),
    _tool_schema("get_daily_stats", "Получить дневную статистику.", {}),
    _tool_schema("get_balance", "Получить текущий баланс и spend-cap тенанта.", {}),
    _tool_schema("get_billing_ledger", "Получить историю списаний (дебет/кредит).", {"limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
    _tool_schema("check_spend_cap", "Проверить, хватит ли бюджета на задачу с указанной стоимостью.", {"estimated_cost_usd": {"type": "number"}}, ["estimated_cost_usd"]),
]


async def execute_tool(name: str, arguments: dict, api_key: str | None = None) -> str:
    """Вызов ROMA API от имени пользователя: порт 8900 и X-API-Key."""
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key

    routes: dict[str, tuple[str, str]] = {
        "submit_task": ("POST", "/submit"), "get_job_status": ("GET", "/status/{job_id}"),
        "list_jobs": ("GET", "/jobs"), "cancel_job": ("POST", "/cancel/{job_id}"),
        "list_workers": ("GET", "/workers"), "drain_worker": ("POST", "/workers/{worker_id}/drain"),
        "slurm_status": ("GET", "/slurm/status/{slurm_job_id}"), "slurm_cancel": ("POST", "/slurm/cancel/{slurm_job_id}"),
        "get_usage": ("GET", "/usage"), "create_checkout_session": ("POST", "/billing/create-checkout-session"),
        "get_daily_stats": ("GET", "/stats/daily"),
        "get_balance": ("GET", "/billing/balance"), "get_billing_ledger": ("GET", "/billing/ledger"),
        "check_spend_cap": ("GET", "/billing/spend-cap"),
    }
    if name not in routes:
        return json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False)

    method, template = routes[name]
    try:
        path = template.format(**arguments)
        payload = arguments if method == "POST" else None
        params = arguments if method == "GET" and "{" not in template else None
        async with httpx.AsyncClient(base_url=ROMA_INTERNAL_BASE_URL, timeout=45.0) as http:
            response = await http.request(method, path, json=payload, params=params, headers=headers)
        if response.status_code >= 400:
            return json.dumps({"error": f"HTTP {response.status_code}", "detail": response.text[:800]}, ensure_ascii=False)
        try:
            return json.dumps(response.json(), ensure_ascii=False, indent=2)
        except Exception:
            return response.text
    except Exception as exc:
        logger.exception("Tool %s failed", name)
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


def _message_dict(message: Any) -> dict:
    if isinstance(message, dict):
        return message
    return {"role": message.role, "content": message.content}


async def stream_with_tools(message: str, history: list[ChatMessage], api_key: str | None = None, name: str | None = None):
    """Стримит ответ DeepSeek и выполняет собранные tool_calls до финального ответа."""
    if message.strip().lower() in {"помощь", "help"}:
        yield (
            "Привет! Я AI-ассистент ROMA.\n\n"
            "Просто пиши обычным языком, например:\n"
            "• Запусти задачу python train.py\n"
            "• Покажи мои задачи\n"
            "• Какие воркеры свободны?\n"
            "• Сколько я потратил?\n"
            "• Отмени задачу abc-123\n\n"
            "Enter — отправить\n"
            "Shift+Enter — новая строка\n"
            "Stop — остановить ответ\n"
            "🗑 — очистить историю"
        )
        return
    if DEEPSEEK_CLIENT is None:
        yield "DeepSeek не настроен. Добавьте DEEPSEEK_API_KEY в секреты сервиса."
        return

    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if name:
        messages.append({"role": "system", "content": f"Пользователя зовут {name}. Обращайся к нему по имени, когда это уместно."})
    messages.extend(_message_dict(item) for item in history[-10:])
    messages.append({"role": "user", "content": message})

    for iteration in range(6):
        stream = None
        last_error: Exception | None = None
        selected_model = DEEPSEEK_MODEL
        for model in (DEEPSEEK_MODEL,):
            try:
                stream = await DEEPSEEK_CLIENT.chat.completions.create(
                    model=model, messages=messages,
                    tools=ROMA_TOOLS, tool_choice="auto", temperature=0.25, max_tokens=2200, stream=True,
                )
                selected_model = model
                selected_model = model
                break
            except Exception as exc:
                last_error = exc
                logger.warning("DeepSeek model %s failed on tool loop %d: %s", model, iteration + 1, exc)

        if stream is None:
            logger.error("DeepSeek failed on tool loop %d: %s", iteration + 1, last_error)
            yield "\n\n❌ DeepSeek временно недоступен. Попробуйте ещё раз позже."
            return

        text_parts: list[str] = []
        tool_calls: dict[int, dict] = {}
        async for chunk in stream:
            for choice in getattr(chunk, "choices", []) or []:
                delta = getattr(choice, "delta", None)
                if not delta:
                    continue
                if delta.content:
                    text_parts.append(delta.content)
                    yield delta.content
                for call_delta in (delta.tool_calls or []):
                    index = call_delta.index
                    call = tool_calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    if call_delta.id:
                        call["id"] = call_delta.id
                    function = call_delta.function
                    if function:
                        call["function"]["name"] += function.name or ""
                        call["function"]["arguments"] += function.arguments or ""

        if not tool_calls:
            return

        messages.append({"role": "assistant", "content": "".join(text_parts) or None, "tool_calls": list(tool_calls.values())})
        for call in tool_calls.values():
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            logger.info("Chat tool call: %s", call["function"]["name"])
            result = await execute_tool(call["function"]["name"], args, api_key)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})

    yield "\n\nДостигнут лимит последовательных вызовов инструментов."


@app.post("/api/chat/stream")
async def chat_stream(
    request: ChatRequest,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    authorization: str | None = Header(None),
):
    """Потоковый ROMA AI endpoint; X-API-Key имеет приоритет над Bearer."""
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Сообщение пустое")
    api_key = x_api_key
    if not api_key and authorization:
        api_key = authorization[7:].strip() if authorization.lower().startswith("bearer ") else authorization.strip()
    logger.info("Chat request: message=%r history=%d api_key_present=%s", message[:100], len(request.history), bool(api_key))
    return StreamingResponse(
        stream_with_tools(message, request.history[-10:], api_key, request.name.strip() if request.name else None),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-cache, no-store", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.get("/stats/daily")
async def daily_stats(request: Request):
    """Daily job/GPU stats from PostgreSQL."""
    rows = db.get_daily_stats()
    return {"stats": rows}

@app.get("/health")
async def health():
    from billing.pg_connection import pg_health
    pg_status = pg_health()
    return {
        "status": "ok",
        "pg": pg_status["connected"],
        "pg_detail": pg_status,
        "version": "2.1.0",
    }


@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ============================================
# ENDPOINTS — DecisionOS: /submit via Gate + PG
# ============================================

# ── Load .env at startup ─────────────────────────────────────────
import os as _os
from pathlib import Path as _Path
_env_path = _Path(__file__).parent / ".env"
if _env_path.exists():
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _key, _val = _line.split('=', 1)
                _key = _key.strip()
                _val = _val.strip().strip('"').strip("'")
                if _key not in _os.environ:
                    _os.environ[_key] = _val

@limiter.limit("30/minute")
@app.post("/submit", response_model=RomaTaskResponse, status_code=202, dependencies=[Depends(verify_api_key)])
async def submit_task(payload: RomaTaskInput, request: Request, key_info: dict = Depends(verify_api_key)):
    global queue_depth
    tenant_id = key_info["tenant_id"]

    gate = _get_gate()

    # Build DecisionRequest
    dreq = DecisionRequest(
        tenant_id=tenant_id,
        request_type="job_submit",
        payload=payload.model_dump(),
        idempotency_key=getattr(payload, "idempotency_key", None),
    )

    # Evaluate through Gate
    decision = gate.evaluate(tenant_id=tenant_id, payload=payload.model_dump())
    if decision.result.value != "allowed":
        logger.warning("decision.denied tenant=%s reason=%s", tenant_id, decision.reason)
        raise HTTPException(status_code=402, detail=decision.reason)

    job_id = str(uuid.uuid4())
    queue_depth += 1
    roma_queue_depth.labels(tenant_id=tenant_id).set(queue_depth)

    # Persist execution job in PG
    db.insert_execution_job(
        job_id=job_id,
        decision_id=str(uuid.uuid4()),  # GateDecision has no decision_id
        tenant_id=tenant_id,
        status="queued",
        payload=payload.model_dump(),
    )

    # Increment usage
    # Dispatch to execution backend (local/vastai/slurm/ray)
    backend_protocol = "rom"
    backend_target = f"rom://local/{job_id}"
    try:
        backend_result = await dispatch_job(
            job_id=job_id,
            tenant_id=tenant_id,
            payload=payload.model_dump(),
        )
        backend_protocol = backend_result.get("protocol", "rom")
        backend_target = backend_result.get("target", f"rom://local/{job_id}")
    except Exception as exc:
        logger.warning("backend.dispatch.failed tenant=%s job=%s: %s", tenant_id, job_id, exc)

    gpu_sec = 300 if payload.gpu_required else 0
    _increment_usage(tenant_id, gpu_sec)

    # Audit: job.created
    try:
        on_job_created(tenant_id, job_id, decision.decision_id)
    except Exception as exc:
        logger.warning("audit.job_created failed tenant=%s job=%s: %s", tenant_id, job_id, exc)

    queue_depth -= 1
    roma_queue_depth.labels(tenant_id=tenant_id).set(queue_depth)
    roma_jobs_total.labels(tenant_id=tenant_id).inc()

    ten_jobs = db.list_tenant_jobs(tenant_id, limit=100)
    roma_jobs_active.labels(tenant_id=tenant_id).set(len(ten_jobs))

    roma_jobs_total.labels(tenant_id=tenant_id).inc()
    ten_jobs = db.list_tenant_jobs(tenant_id, limit=100)
    roma_jobs_active.labels(tenant_id=tenant_id).set(len(ten_jobs))

    return RomaTaskResponse(
        status="queued",
        job_id=job_id,
        tenant_id=tenant_id,
        roma_dispatch={"protocol": backend_protocol, "target": backend_target},
        dag=["validate", "dispatch", "execute", "commit"],
        estimated_resources={"cpu_cores": 2, "memory_mb": 512, "gpu": 1 if payload.gpu_required else 0},
        gpu_required=payload.gpu_required,
    )


@app.get("/status/{job_id}", response_model=RomaStatusResponse, dependencies=[Depends(verify_api_key)])
async def get_status(job_id: str, key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]
    job = db.get_execution_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="Job not found")
    return RomaStatusResponse(
        job_id=job_id,
        status=job["status"],
        created_at=job["created_at"],
        started_at=job.get("started_at"),
        completed_at=job.get("completed_at"),
        error=job.get("error"),
    )


@app.post("/cancel/{job_id}", dependencies=[Depends(verify_api_key)])
async def cancel_job(job_id: str, key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]
    job = db.get_execution_job(job_id)
    if not job or job.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="Job not found")
    db.update_execution_job(job_id, status="cancelled")
    try:
        write_event(tenant_id, "job.cancelled", "job", job_id, {})
    except Exception:
        pass
    return {"status": "cancelled", "job_id": job_id}


@app.post("/complete/{job_id}", dependencies=[Depends(verify_api_key)])
async def complete_job(job_id: str, key_info: dict = Depends(verify_api_key)):
    job = db.get_execution_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    tenant_id = key_info["tenant_id"]

    # FIXED: real billing — compute actual GPU seconds from job duration
    actual_duration_s = 0
    import time
    started_at = job.get("started_at")
    if started_at:
        try:
            started_dt = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
            actual_duration_s = (datetime.utcnow() - started_dt.replace(tzinfo=None)).total_seconds()
        except Exception:
            actual_duration_s = 300
    gpu_sec = max(actual_duration_s, 0)
    _increment_usage(tenant_id, gpu_sec, cost_only=True)

    # Cleanup backend instance (Vast.ai destroy etc.)
    try:
        await backend_cancel_job(tenant_id=tenant_id, job_id=job_id)
    except Exception as exc:
        logger.warning("backend.cleanup.failed tenant=%s job=%s: %s", tenant_id, job_id, exc)

    db.update_execution_job(job_id, status="completed", completed_at=datetime.utcnow().isoformat())
    return {"status": "completed", "job_id": job_id}


@app.get("/jobs", dependencies=[Depends(verify_api_key)])
async def list_jobs(key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]
    my_jobs = db.list_jobs(tenant_id, limit=100)
    return {
        "rom_version": "1.0.0",
        "tenant_id": tenant_id,
        "queue": len(my_jobs),
        "jobs": my_jobs[-10:],
        "execution_modes": ["k8s_job", "k8s_persistent", "atom_cluster", "batch", "vastai", "local"],
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
    db.insert_job_raw(job_id, tenant_id, "atom_cluster_managed", job)
    return job


# ============================================
# ENDPOINTS — Usage (PG-backed)
# ============================================

@app.get("/usage", dependencies=[Depends(verify_api_key)])
async def get_usage(key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]
    t = db.get_tenant(tenant_id)
    plan_name = t.get("plan", "free") if t else "free"
    plan = PLANS.get(plan_name, PLANS.get("free", {}))
    usage_data = db.get_tenant_usage_db(tenant_id)
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
                f"Selected plan: {plan_name} ({plan_example.get('amount', 0)} RUB/session)"
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
# BILLING — Ledger & Spend-Cap (v2.1.0)
# ============================================

@limiter.limit("30/minute")
@app.get("/billing/ledger")
async def get_billing_ledger(
    request: Request,
    limit: int = 20,
    key_info: dict = Depends(verify_api_key),
):
    """История списаний (дебет/кредит) для текущего tenant."""
    tenant_id = key_info["tenant_id"]
    entries = billing_ledger.get_tenant_entries(tenant_id)
    entries = entries[-limit:] if limit > 0 else entries
    balance = billing_ledger.get_balance(tenant_id)
    plan_name = key_info.get("plan", "free")
    plan = PLANS.get(plan_name, PLANS.get("free", {}))
    return {
        "tenant_id": tenant_id,
        "plan": plan_name,
        "balance_usd": round(balance, 6),
        "spend_cap_usd": plan.get("spend_cap_usd", 0),
        "entries": [
            {
                "type": e["type"],
                "amount": e["amount"],
                "timestamp": e.get("timestamp", 0),
                "description": e.get("description", ""),
                "metadata": e.get("metadata", {}),
            }
            for e in entries
        ],
        "total_entries": len(entries),
    }


@limiter.limit("30/minute")
@app.get("/billing/spend-cap")
async def check_spend_cap_endpoint(
    request: Request,
    estimated_cost_usd: float = 0.0,
    key_info: dict = Depends(verify_api_key),
):
    """Проверить, хватит ли бюджета на задачу с указанной стоимостью."""
    tenant_id = key_info["tenant_id"]
    plan_name = key_info.get("plan", "free")
    plan = PLANS.get(plan_name, PLANS.get("free", {}))
    cap = plan.get("spend_cap_usd", 0)
    if cap <= 0:
        return {
            "tenant_id": tenant_id,
            "plan": plan_name,
            "spend_cap_usd": cap,
            "current_balance": 0.0,
            "estimated_cost": estimated_cost_usd,
            "allowed": True,
            "reason": "No spend-cap (enterprise/unlimited)",
            "remaining": "unlimited",
        }
    balance = billing_ledger.get_balance(tenant_id)
    projected = balance + estimated_cost_usd
    allowed = projected <= cap
    pct = round(balance / cap * 100, 1) if cap > 0 else 0
    return {
        "tenant_id": tenant_id,
        "plan": plan_name,
        "spend_cap_usd": cap,
        "current_balance": round(balance, 6),
        "estimated_cost": estimated_cost_usd,
        "allowed": allowed,
        "remaining": round(max(0, cap - balance), 6),
        "usage_pct": pct,
        "reason": "" if allowed else f"Spend cap exceeded: ${balance:.4f}/${cap:.2f} ({pct}%). Job ${estimated_cost_usd:.6f} exceeds cap.",
    }




# ============================================

@limiter.limit("30/minute")
@app.get("/billing/balance")
async def get_balance_endpoint(
    request: Request,
    key_info: dict = Depends(verify_api_key),
):
    """Текущий баланс, план и spend-cap тенанта (короткий ответ для AI)."""
    tenant_id = key_info["tenant_id"]
    plan_name = key_info.get("plan", "free")
    plan = PLANS.get(plan_name, PLANS.get("free", {}))
    cap = plan.get("spend_cap_usd", 0)
    balance = billing_ledger.get_balance(tenant_id)
    pct = round(balance / cap * 100, 1) if cap > 0 else 0
    return {
        "tenant_id": tenant_id,
        "plan": plan_name,
        "balance_usd": round(balance, 6),
        "spend_cap_usd": cap,
        "usage_pct": pct,
        "remaining": round(max(0, cap - balance), 6) if cap > 0 else "unlimited",
        "limits": {
            "max_jobs_per_month": plan.get("max_jobs_per_month", 50),
            "max_gpu_seconds": plan.get("max_gpu_seconds", 0),
        },
    }



# ============================================
# ADMIN — Test Alerts (internal)
# ============================================

class TestAlertRequest(BaseModel):
    """Запрос на тестовую отправку алерта."""
    channel: str | None = None  # telegram, discord, email или None = все
    message: str = "🧪 Тестовый алерт ROMA Execution Bridge v2.1.0"

@limiter.limit("5/minute")
@app.post("/admin/test-alert")
async def test_alert(
    request: Request,
    body: TestAlertRequest = TestAlertRequest(),
    key_info: dict = Depends(verify_api_key),
):
    """Отправить тестовый алерт через заданный канал (или все)."""
    alert = Alert(
        level=AlertLevel.INFO,
        title="Тестовый алерт ROMA",
        body=body.message,
    )
    alert_dispatcher.send(alert)
    return {
        "sent": True,
        "channel": body.channel or "all",
        "preview": alert.format_markdown().split(chr(10))[0],
    }


# DEMOS — ready-to-run tasks
# ============================================

DEMOS = {
    "demo-pytorch-train": {
        "task": "echo [PT] Epoch 1/2 loss=0.42 && sleep 1 && echo [PT] Epoch 2/2 loss=0.18 && echo TRAINING_COMPLETE",
        "gpu_required": True,
        "priority": 8,
        "execution_mode": "k8s_job",
        "description": "Simple PyTorch model training with gradient descent.",
        "estimated_time": "~2 min",
    },
    "demo-inference": {
        "task": "echo [BERT] Loading model... && sleep 0.5 && echo [BERT] Sentiment: POSITIVE (0.94) NEGATIVE (0.03) && echo INFERENCE_COMPLETE",
        "gpu_required": True,
        "priority": 6,
        "execution_mode": "k8s_job",
        "description": "Batch inference using a pre-trained BERT model.",
        "estimated_time": "~30 sec",
    },
    "demo-batch-processing": {
        "task": "for i in 1 2 3 4 5; do echo [BATCH] Processing chunk $i/5...; sleep 0.3; done && echo BATCH_COMPLETE: 1000 images processed",
        "gpu_required": False,
        "priority": 5,
        "execution_mode": "k8s_job",
        "description": "Mass image processing pipeline — no GPU needed.",
        "estimated_time": "~1 min",
    },
    "demo-gpu-benchmark": {
        "task": "echo [GPU_BENCH] Matrix 4096x4096... && sleep 2 && echo [GPU_BENCH] GFLOPS: 14.2 && echo BENCHMARK_COMPLETE",
        "gpu_required": True,
        "priority": 10,
        "execution_mode": "k8s_job",
        "description": "Raw GPU compute test — measures FLOPS.",
        "estimated_time": "~15 sec",
    },
    "demo-hello-world": {
        "task": "echo HELLO_WORLD from $(hostname) at $(date -u +%Y-%m-%dT%H:%M:%SZ) && echo ROMA_CONNECTIVITY_OK",
        "gpu_required": False,
        "priority": 1,
        "execution_mode": "k8s_job",
        "description": "Simple smoke-test job — prints timestamp and hostname.",
        "estimated_time": "~5 sec",
    },
}


async def submit_job(payload: RomaTaskInput, key_info: dict) -> RomaTaskResponse:
    """Internal job submission (used by demos and other internal callers)."""
    global queue_depth
    tenant_id = key_info["tenant_id"]
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
        gpu_sec = 300 if payload.gpu_required else 0
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

@app.post("/demo/{demo_name}")
async def run_demo(demo_name: str, key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]
    if demo_name not in DEMOS:
        raise HTTPException(status_code=404, detail=f"Demo not found: {demo_name}. Available: {list(DEMOS.keys())}")
    demo = DEMOS[demo_name]
    input_data = {k: v for k, v in demo.items() if k in RomaTaskInput.model_fields}
    payload = RomaTaskInput.model_validate(input_data)
    return await submit_job(payload, key_info)
# ============================================
# SLURM INTEGRATION ENDPOINTS
# ============================================

from scheduler.slurm_plugin import slurm as slurm_plugin
from backends.dispatcher import dispatch_job, backend_cancel_job


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
    return {"rom_version": "1.0.0", "tenant_id": tenant_id, "workers": workers}

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
async def beta_apply(request: Request, payload: dict):
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
from auth.verification import create_verification, verify_token, is_email_verified
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

# ============================================
# AUTH — Email Signup + Verification
# ============================================

VERIFICATION_TOKEN_EXPIRY_HOURS = int(os.environ.get("VERIFICATION_TOKEN_EXPIRY_HOURS", "24"))
VERIFICATION_BASE_URL = os.environ.get("VERIFICATION_BASE_URL", "https://roma-execution-bridge-asurdev.zocomputer.io")

@app.post("/auth/signup", status_code=201)
async def signup(payload: dict, request: Request):
    """Register new user with email/password. Sends verification email."""
    email = (payload.get("email") or "").strip().lower()
    password = (payload.get("password") or "")
    name = (payload.get("name") or email.split("@")[0])
    plan = payload.get("plan", "free")
    
    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    
    existing = db.get_user_by_email(email)
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")
    
    user_id = str(uuid.uuid4())
    tenant_id = f"tenant-{str(uuid.uuid4())[:8]}"
    api_key = f"roma-{str(uuid.uuid4())[:12]}"
    password_hash = hashlib.sha256(password.encode() + user_id.encode()).hexdigest()
    
    token, expires_at = create_verification(user_id)
    from auth.verification import generate_token
    token = generate_token()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=VERIFICATION_TOKEN_EXPIRY_HOURS)).isoformat()
    verification_url = f"{VERIFICATION_BASE_URL}/auth/verify-email?token={token}"
    
    db.seed_tenants({api_key: {"tenant_id": tenant_id, "plan": plan}})
    API_KEYS[api_key] = {"tenant_id": tenant_id, "plan": plan, "email_verified": False}
    db.create_user_with_password(user_id, email, name, tenant_id, api_key, password_hash, token, expires_at)
    
    try:
        email_service.send_verification_email(
            to_email=email,
            tenant_name=name,
            verification_url=verification_url,
            brand={"app_name": "ROMA", "primary_color": "#6366f1"},
            expiry_hours=VERIFICATION_TOKEN_EXPIRY_HOURS,
        )
        logger.info("verification_email_sent", extra={"email": email, "tenant_id": tenant_id})
    except Exception as e:
        logger.warning("verification_email_failed", extra={"email": email, "error": str(e)})
    
    return {
        "status": "pending",
        "message": "Account created. Please check your email to verify your address.",
        "tenant_id": tenant_id,
    }


@app.get("/auth/verify-email")
async def verify_email_endpoint(token: str):
    """Verify email address. Can be called via browser (GET) or API (POST)."""
    result = verify_token(token)
    if not result:
        raise HTTPException(status_code=400, detail="Invalid or expired verification token")
    
    user_id = result["user_id"]
    user = db.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    db.mark_email_verified(user["email"])
    api_key = user.get("api_key", "")
    if api_key and api_key in API_KEYS:
        API_KEYS[api_key]["email_verified"] = True
    
    return {
        "status": "verified",
        "message": "Email verified successfully. Your account is now active.",
        "email": user["email"],
    }


@app.post("/auth/resend-verification")
async def resend_verification(payload: dict):
    """Resend verification email."""
    email = (payload.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")
    
    user = db.get_user_by_email(email)
    if not user:
        raise HTTPException(status_code=404, detail="No account found with this email")
    if user.get("email_verified"):
        return {"status": "already_verified", "message": "Email is already verified"}
    
    token, expires_at = create_verification(user["id"])
    verification_url = f"{VERIFICATION_BASE_URL}/auth/verify-email?token={token}"
    db.update_verification_token(user["id"], token, expires_at)
    
    try:
        email_service.send_verification_email(
            to_email=email,
            tenant_name=user.get("name", email),
            verification_url=verification_url,
            brand={"app_name": "ROMA", "primary_color": "#6366f1"},
            expiry_hours=VERIFICATION_TOKEN_EXPIRY_HOURS,
        )
        logger.info("verification_resent", extra={"email": email})
    except Exception as e:
        logger.warning("resend_verification_failed", extra={"email": email, "error": str(e)})
    
    return {"status": "sent", "message": "Verification email resent. Please check your inbox."}

# OAUTH2 — Google + GitHub Login
# ============================================

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
        logger.info("OAuth redirect → Google")
        return RedirectResponse(url=auth_url, status_code=302)

    elif provider == "github" and GITHUB_CLIENT_ID:
        params = {
            "client_id": GITHUB_CLIENT_ID,
            "redirect_uri": f"{OAUTH_REDIRECT_BASE}/auth/oauth/callback/github",
            "scope": "user:email",
        }
        auth_url = f"https://github.com/login/oauth/authorize?{urlencode(params)}"
        logger.info("OAuth redirect → GitHub")
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
        <div class="sub">Plan: <strong>{plan.get('name', plan_name)}</strong> (${plan.get('spend_cap_usd', 0)}/session)</div>
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
    <span><span class="dot"></span> ROMA v2.1.0 — Phase 0 Pre-Launch</span>
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
    raw_body = await request.body()
    signature = (
        request.headers.get("Content-HMAC")
        or request.headers.get("Content-Hmac")
        or request.headers.get("X-Content-HMAC")
        or ""
    )

    if not CLOUDPAYMENTS_ENABLED or cloudpayments_client is None:
        return {"code": 0}

    if not cloudpayments_client.verify_webhook(raw_body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = payload.get("OperationType") or payload.get("Status") or ""
    tenant_id = (
        payload.get("AccountId")
        or (payload.get("Data") or {}).get("tenant_id")
        or ""
    )
    invoice_id = payload.get("InvoiceId", "")
    data = payload.get("Data") or {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    plan = data.get("plan", "")

    if not invoice_id:
        return {"code": 0}

    if db.is_invoice_processed(invoice_id):
        return {"code": 0}

    status = payload.get("Status", "")

    try:
        if event_type in ("Payment", "Pay", "Completed") or status in ("Completed", "Authorized"):
            if plan in ("pro", "enterprise") and tenant_id:
                db.update_tenant_subscription(tenant_id, invoice_id, "", "active", plan, None)
                db.mark_invoice_processed(invoice_id, event_type, tenant_id)
            else:
                db.mark_invoice_processed(invoice_id, event_type or "payment_no_plan", tenant_id)

        elif event_type == "Recurrent" and status == "Completed":
            if plan and tenant_id:
                db.update_tenant_subscription(tenant_id, invoice_id, "", "active", plan, None)
                db.mark_invoice_processed(invoice_id, event_type, tenant_id)
            else:
                db.mark_invoice_processed(invoice_id, event_type or "recurrent_no_plan", tenant_id)

        elif event_type in ("Fail", "Declined") or status in ("Declined", "Cancelled"):
            db.set_tenant_inactive(tenant_id)
            db.mark_invoice_processed(invoice_id, event_type, tenant_id)

        elif event_type in ("Cancel", "Unsubscribe"):
            db.update_tenant_subscription(tenant_id, "", "", "canceled", "free", None)
            db.mark_invoice_processed(invoice_id, event_type, tenant_id)

        else:
            db.mark_invoice_processed(invoice_id, event_type or "ignored", tenant_id)

    except Exception:
        logger.exception(f"Failed to process CloudPayments webhook {invoice_id}")
        raise HTTPException(status_code=500, detail="Processing error")

    return {"code": 0}

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

def _get_client_ip(request: Request) -> str:
    """Get real client IP, respecting proxy headers (X-Forwarded-For, X-Real-IP)."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    xri = request.headers.get("X-Real-IP", "")
    if xri:
        return xri.strip()
    return request.client.host if request.client else "unknown"


def _ip_allowed(client_ip: str) -> bool:
    """Check if client IP is in the ADMIN_IP_ALLOWLIST (supports CIDR, comma-separated, * for any)."""
    allowlist = ADMIN_IP_ALLOWLIST.strip()
    if allowlist == "*":
        return True
    if not allowlist:
        return False
    try:
        client = ipaddress.ip_address(client_ip)
    except ValueError:
        logger.warning(f"Admin IP check: invalid client IP \'{client_ip}\'")
        return False
    for entry in allowlist.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            if entry == "::1" and client_ip in ("::1", "127.0.0.1"):
                return True
            network = ipaddress.ip_network(entry, strict=False)
            if client in network:
                return True
        except ValueError:
            logger.warning(f"Admin IP allowlist: invalid entry \'{entry}\'")
            continue
    return False


def _admin_only(request: Request) -> dict:
    """Verify admin access — IP allowlist + valid API key + tenant-demo."""

    client_ip = _get_client_ip(request)
    if not _ip_allowed(client_ip):
        logger.warning(f"Admin access denied — IP not in allowlist: {client_ip}")
        raise HTTPException(status_code=403, detail=f"Access denied from {client_ip}")

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


@app.get("/admin/backends", dependencies=[Depends(verify_api_key)])
async def admin_backends(key_info: dict = Depends(verify_api_key)):
    """List available execution backends and their status."""
    from backends.dispatcher import list_backends
    return {
        "active_backend": os.getenv("ROMA_EXECUTION_BACKEND", "local"),
        "backends": list_backends(),
    }


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

        invitation_link = "https://roma-execution-bridge-asurdev.zocomputer.io/dashboard?api_key=roma-demo-key-2026"

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


@app.on_event("startup")
async def startup_event():
    try:
        billing_ledger._pg._ensure_pool()
        logger.info("PG pool initialized on startup")
    except Exception as e:
        logger.warning("Failed to init PG pool on startup: %s", e)


@app.on_event("shutdown")
async def shutdown_event():
    try:
        from billing.pg_connection import shutdown_pg
        shutdown_pg()
        from db_adapter import close_pg_pool
        close_pg_pool()
        logger.info("PG pool released on shutdown")
    except Exception as e:
        logger.warning("Failed to close PG pool: %s", e)


