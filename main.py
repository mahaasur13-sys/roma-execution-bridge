"""
ROMA Execution Bridge – FastAPI + Pydantic v2
Multi-tenant execution platform with API-Key auth, tenant isolation, and billing.
"""
# ── Load .env BEFORE all imports ─────────────────────────────────
from env_loader import load_env

load_env()


import asyncio
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

# A1 — shared singletons/helpers (re-exported so main.* names stay intact)
from deps import (
    API_KEYS,
    ADMIN_IP_ALLOWLIST,
    verify_api_key,
    _get_client_ip,
    _ip_allowed,
    _admin_only,
    alert_dispatcher,
    limiter,
    billing_ledger,
    PLANS,
)

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
from alerts import Alert, AlertLevel

# === BILLING SINGLETONS (v2.1.0) ===
from billing.pg_ledger import PGUnavailableError
from billing.finalize import _increment_usage, finalize_job_billing, metering_engine

from billing.execution_worker import init_worker, execute_and_bill, bill_job, poll_and_execute

_http_request_count = 0
_billing_event_count = 0


def _check_spend_cap(tenant_id: str, estimated_cost: float, plan_name: str = "free"):
    """Проверка spend-cap ПЕРЕД созданием job."""
    plan = PLANS.get(plan_name, PLANS.get("free", {}))
    cap = plan.get("spend_cap_usd", 0)
    if cap <= 0:
        return True, "Free plan: no spend-cap. Upgrade to Start ($5 cap) for GPU access."
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



def _check_limits(tenant_id: str) -> tuple[bool, str]:
    """Quota check for the internal/demo submit path. Returns (allowed, reason)."""
    t = db.get_tenant(tenant_id)
    plan_name = t.get("plan", "free") if t else "free"
    plan = PLANS.get(plan_name, PLANS.get("free", {}))
    max_jobs = plan.get("max_jobs_per_month", 50)
    if max_jobs < 0:
        return True, ""
    used = db.count_jobs_for_tenant(tenant_id)
    if used >= max_jobs:
        return False, f"Monthly job limit reached: {used}/{max_jobs}"
    return True, ""


def _get_tenant_usage(tenant_id: str) -> dict:
    """Usage summary for the dashboard (total_jobs + total_gpu_seconds)."""
    return db.get_tenant_usage_db(tenant_id)

from pydantic import BaseModel, Field, ConfigDict
from starlette.requests import Request
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, Response, StreamingResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded


# ============================================
# JSON LOGGING
# ============================================

class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
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
    
    # Check email verification for API endpoints (skip auth endpoints and admin keys)
    _ADMIN_KEYS = {k.strip() for k in os.getenv("ROMA_ADMIN_KEYS", "").split(",") if k.strip()}
    if x_api_key not in _ADMIN_KEYS:
        verif_status = is_email_verified(x_api_key)
        if not verif_status:
            raise HTTPException(status_code=403, detail="Email not verified. Please verify your email first.")
    return tenant

# ============================================
# PLANS & USAGE — DecisionOS PG-backed
# ============================================

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
        webhook_secret=os.environ.get("CLOUDPAYMENTS_WEBHOOK_SECRET", ""),
    )
    cloudpayments_client = CloudPaymentsClient(_cp_cfg)


import httpx



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
# MODELS (extracted to models/app.py — A1)
# ============================================

from models.app import (
    RomaTaskInput,
    RomaTaskResponse,
    RomaStatusResponse,
    CheckoutRequest,
    CheckoutResponse,
    UsageResponse,
    ChatMessage,
    ChatRequest,
    TestAlertRequest,
)

# ============================================
# APP
# ============================================

app = FastAPI(
    title="ROMA Execution Platform",
    version="2.1.0",
)
app.state.limiter = limiter

# CORS (C5) — allowlist from CORS_ALLOW_ORIGINS
def _cors_allowed_origins() -> list[str]:
    """Resolve CORS origins from CORS_ALLOW_ORIGINS (comma-separated).

    - explicit list -> used as-is (trimmed, empty entries dropped)
    - production (ENV/ROMA_ENV=production) + empty -> fail-closed []
    - non-prod + empty -> localhost only
    """
    raw = os.environ.get("CORS_ALLOW_ORIGINS", "")
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if origins:
        return origins
    production = (
        os.environ.get("ENV", "").strip().lower() == "production"
        or os.environ.get("ROMA_ENV", "").strip().lower() == "production"
    )
    if production:
        return []  # fail-closed
    return ["http://127.0.0.1:3080", "http://localhost:3080"]


def _cors_allow_credentials(origins: list[str]) -> bool:
    """Credentials are allowed only when the origin list has no wildcard."""
    return "*" not in origins


_cors_origins = _cors_allowed_origins()
_cors_credentials_enabled = _cors_allow_credentials(_cors_origins)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_credentials_enabled,
    allow_methods=["*"],
    allow_headers=["*"],
)

# DecisionOS Week 2 — v1 API routes
from routes.v1_router import router as v1_router
from router_decisions import router as decisions_router
from router_jobs import router as jobs_router
from crypto_payments.router import router as crypto_router
from crypto_payments.wallets.router import router as wallets_router
from support_chat.router import router as support_router
from routers.admin import router as admin_router
from routers.webhooks import router as webhooks_router
from routers.auth import router as auth_router
from routers.billing import router as billing_router
from routers.public_jobs import router as public_jobs_router
from routers.submit import router as submit_router
from routers.beta import router as beta_router
app.include_router(v1_router)
app.include_router(decisions_router)
app.include_router(jobs_router)
app.include_router(crypto_router)
app.include_router(wallets_router)
app.include_router(support_router)
app.include_router(admin_router)
app.include_router(webhooks_router)
app.include_router(auth_router)
app.include_router(billing_router)
app.include_router(public_jobs_router)
app.include_router(submit_router)
app.include_router(beta_router)
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
jobs: dict = {}  # in-memory job registry for the demo/dashboard path
roma_requests_total = Counter("roma_requests_total", "Total HTTP requests", ["endpoint", "method", "status"])
roma_request_duration = Histogram("roma_request_duration_seconds", "Request duration in seconds", ["endpoint", "method"])

# Business metrics (P2-4)
roma_billing_events = Counter("roma_billing_events_total", "Billing events", ["event_type", "plan"])
roma_errors_total = Counter("roma_errors_total", "Errors by endpoint", ["endpoint", "status_code"])

from monitoring.verification_metrics import (
    roma_email_verification_total, roma_email_send_total,
    roma_email_resend_total, roma_unverified_api_key_blocked_total,
    roma_verification_token_expired_total, roma_verification_token_invalid_total,
)
VERIFICATION_METRICS_LOADED = True
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
# FIXME(P0-1): the key must come ONLY from env / secret manager. The committed
# config/deepseek_key.txt fallback was removed — never read secrets from repo files.
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
    """Daily job/GPU stats (last 7 days)."""
    return db.get_daily_stats()

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


@app.get("/ready")
async def ready():
    """Readiness probe — PG connected + pool healthy (fail-closed)."""
    from fastapi.responses import JSONResponse
    from billing.pg_connection import pg_health
    pg_status = pg_health()
    pg_ok = bool(pg_status.get("connected"))
    pool_ok = bool(pg_status.get("pool_configured")) and pg_status.get("status") == "healthy"
    ready = pg_ok and pool_ok and int(pg_status.get("error_count", 0)) < 5
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "pg": pg_ok,
            "pool": pg_status.get("pool_size", "0/10"),
            "reconnect_count": pg_status.get("reconnect_count", 0),
            "error_count": pg_status.get("error_count", 0),
            "version": "2.1.0",
        },
    )

@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ============================================
# ENDPOINTS — DecisionOS: /submit via Gate + PG
# ============================================

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
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload.model_dump(),
        }
        jobs[job_id] = job
        queue_depth -= 1
        roma_queue_depth.labels(tenant_id=tenant_id).set(queue_depth)
        roma_jobs_total.labels(tenant_id=tenant_id).inc()
        # No debit at demo submit — billing is finalized exactly once via
        # finalize_job_billing() (see /complete and execute_and_bill).
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


from auth.sessions import get_session
from auth.verification import is_email_verified
from starlette.responses import RedirectResponse

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


@app.on_event("startup")
async def startup_event():
    try:
        billing_ledger._pg._ensure_pool()
        try:
            init_worker()
            asyncio.create_task(poll_and_execute())
            logger.info("execution_worker initialized")
        except Exception as e:
            logger.warning("Failed to init execution_worker: %s", e)
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


