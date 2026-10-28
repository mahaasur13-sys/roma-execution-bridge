"""Shared runtime dependencies extracted from ``main.py`` (A1 — deps + admin).

Holds the singletons and helpers that both ``main.py`` and the extracted
``APIRouter`` modules need, so routers can import from here instead of
importing ``main`` (which would create a circular import).

Everything here is a verbatim cut-paste from ``main.py`` — no behaviour change.
``main.py`` re-imports these names (so any test that patches ``main.X`` keeps
working). There is no ``create_app`` here.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import os

import config
import db_adapter as db
import plan_source
from fastapi import Header, HTTPException, Request
from slowapi import Limiter

from alerts import AlertDispatcher
from billing.pg_ledger import PGBillingLedger as BillingLedger
from billing.spend_caps import MONEY_POLICY

# ── Logging ────────────────────────────────────────────────────────────
logger = logging.getLogger("roma")

# ── Client address ─────────────────────────────────────────────────────
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def client_ip(request: Request) -> str:
    """Real client address, proxy-aware only when a proxy really sits in front.

    X-Forwarded-For is client-writable, so it is read only when the socket peer
    is loopback (the proxy runs next to us) and ROMA_TRUST_PROXY=1. The rightmost
    entry is the one the nearest proxy appended; anything left of it came from
    the caller and can be spoofed.
    """
    peer = request.client.host if request.client else ""
    if peer in _LOOPBACK_HOSTS and os.getenv("ROMA_TRUST_PROXY", "") == "1":
        xff = request.headers.get("X-Forwarded-For", "")
        if xff.strip():
            return xff.split(",")[-1].strip()
    return peer or "unknown"


# ── Rate limiting ──────────────────────────────────────────────────────
limiter = Limiter(key_func=client_ip)

# ── Alerts ─────────────────────────────────────────────────────────────
alert_dispatcher = AlertDispatcher()

# ── In-memory API key registry (shared mutable dict) ──────────────────
# Keys are sha256 digests of the raw API key — the raw key is never stored here,
# so a heap dump or a debug repr cannot leak a working credential.
API_KEYS: dict[str, dict] = {}


def key_digest(raw_key: str) -> str:
    """sha256 hex digest of a raw API key — the only form kept in ``API_KEYS``."""
    if not raw_key:
        raise ValueError("empty API key cannot be hashed")
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def lookup_key(raw_key: str | None) -> dict | None:
    """Resolve a raw API key to tenant info; the database is the authority.

    ``API_KEYS`` is only a cache of keys this process minted, so it must not
    outvote a revocation made elsewhere: a digest still sitting in the registry
    while its tenant row already carries another ``api_key_hash`` (see
    ``scripts/rotate_user_key.py``) is dead and resolves to ``None``. Entries
    whose tenant row does not exist at all — static/admin keys and test doubles —
    keep working as before.
    """
    if not raw_key:
        return None
    tenant = db.find_tenant_by_key(raw_key)
    if tenant:
        return {"tenant_id": tenant["tenant_id"], "plan": tenant.get("plan")}
    registered = API_KEYS.get(key_digest(raw_key))
    if registered is None:
        return None
    tenant_id = registered.get("tenant_id")
    if not tenant_id or db.get_tenant(tenant_id) is None:
        return registered
    return None


def register_key(raw_key: str, info: dict) -> None:
    """Register a raw API key; only its digest reaches ``API_KEYS``."""
    API_KEYS[key_digest(raw_key)] = info


def revoke_key(digest: str) -> bool:
    """Drop a digest from ``API_KEYS``; ``True`` when an entry was removed."""
    return API_KEYS.pop(digest, None) is not None


# ── Admin access ───────────────────────────────────────────────────────
ADMIN_IP_ALLOWLIST = os.environ.get(
    "ADMIN_IP_ALLOWLIST",
    "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
)

# ── Billing ─────────────────────────────────────────────────────────────
billing_ledger = BillingLedger()

# G-MONEY-CAP-LITERALS: денежная политика живёт в `billing/spend_caps.py` — там же её
# читает сверка ledger. Здесь только алиас, чтобы `deps.plan_config` не менялся.
_MONEY_POLICY: dict[str, dict[str, float]] = MONEY_POLICY


def plan_config(plan_name: str | None) -> dict:
    """Лимиты тира для API-пути: квоты — из `plan_source`, деньги — из money-политики.

    Собственной таблицы квот и собственной арифметики здесь нет: тир, отсутствующий
    в источнике, — отказ `PlanSourceError` (fail-closed), а не молчаливый free-дефолт.
    Потребители обязаны превратить этот отказ в вердикт `GATE_UNAVAILABLE`, а не в
    разрешение.
    """
    limits = plan_source.plan_limits(plan_name)
    money = _MONEY_POLICY.get((plan_name or "").strip().lower(), {})
    return {
        "max_jobs_per_month": limits.jobs_per_month,
        "gpu_s_per_job": limits.gpu_s_per_job,
        "gpu_s_per_month": limits.gpu_s_per_month,
        "gpu_hours_per_month": limits.gpu_hours_per_month,
        "spend_cap_usd": money.get("spend_cap_usd", 0.0),
        "overage_rate": money.get("overage_rate", 0.0),
    }


def _derive_plans() -> dict:
    """PLANS — вывод из единственного источника, а не собственная таблица квот.

    Оставлен для дисплеев и совместимости; принуждающие потребители обязаны
    пользоваться `plan_config`, чтобы отсутствующий тир получался отказом.
    """
    return {name: plan_config(name) for name in plan_source.load_plans()}


PLANS: dict = _derive_plans()


def admin_keys() -> set[str]:
    """Admin API keys, from ``ROMA_ADMIN_KEYS`` only (comma-separated).

    Single source for both ``deps.verify_api_key`` and ``main.verify_api_key``.
    Absent or empty env means no admin keys: admin bypass is then impossible
    (fail-closed). There is no built-in default — never reintroduce one.
    """
    return {k.strip() for k in os.getenv("ROMA_ADMIN_KEYS", "").split(",") if k.strip()}


def verify_api_key(x_api_key: str = Header(None)) -> dict:
    """Validate API key and return tenant info: {tenant_id, name, tier, api_key}."""
    if not x_api_key:
        raise HTTPException(
            status_code=401,
            detail=f"Missing X-API-Key header. Request a key at {config.PUBLIC_BASE_URL}",
        )
    tenant = db.find_tenant_by_key(x_api_key)
    if not tenant:
        raise HTTPException(status_code=401, detail="Invalid API key")
    tenant_id = (tenant.get("tenant_id") or "").strip()
    if not tenant_id:
        # A key that resolves to an empty/whitespace tenant is treated the same
        # as an invalid key — never a successful login.
        raise HTTPException(status_code=401, detail="Invalid API key")
    tenant["tenant_id"] = tenant_id
    tenant["api_key"] = x_api_key

    # Check email verification for API endpoints (skip auth endpoints and admin keys)
    if not _admin_key_valid(x_api_key):
        # Deferred import: is_email_verified lives in main.py (auth cluster, out
        # of scope for this split). Reading main.is_email_verified at call time
        # keeps the existing monkeypatch in tests/test_p0_security.py effective.
        import main

        verif_status = main.is_email_verified(x_api_key)
        if not verif_status:
            raise HTTPException(
                status_code=403,
                detail="Email not verified. Please verify your email first.",
            )
    return tenant


def _get_client_ip(request: Request) -> str:
    """Admin allowlist view of the caller — same rules as the rate limiter."""
    return client_ip(request)


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
        logger.warning(f"Admin IP check: invalid client IP '{client_ip}'")
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
            logger.warning(f"Admin IP allowlist: invalid entry '{entry}'")
            continue
    return False


def _admin_key_valid(candidate: str) -> bool:
    """Constant-time membership check against ``ROMA_ADMIN_KEYS``.

    Fail-closed: an empty/absent env yields no keys, so nothing is valid. Every
    configured key is compared (no early exit) via ``hmac.compare_digest``.
    """
    if not candidate:
        return False
    keys = admin_keys()
    if not keys:
        return False
    matched = False
    for key in keys:
        if hmac.compare_digest(candidate, key):
            matched = True
    return matched


def _admin_only(request: Request) -> dict:
    """Verify admin access — ``ROMA_ADMIN_KEYS`` key + IP allowlist.

    Tenant API keys (including ``tenant-demo``) grant no admin rights. The key
    is read from the ``X-API-Key`` header only; a ``?api_key=`` query parameter
    is not accepted (it leaks into access logs and Referer).
    """
    api_key_raw = request.headers.get("X-API-Key", "") or ""
    if not api_key_raw:
        raise HTTPException(status_code=401, detail="Admin API key required")

    if not _admin_key_valid(api_key_raw):
        logger.warning("Admin access denied — key not in ROMA_ADMIN_KEYS")
        raise HTTPException(status_code=403, detail="Admin access requires an admin API key")

    caller = _get_client_ip(request)
    if not _ip_allowed(caller):
        logger.warning(f"Admin access denied — IP not in allowlist: {caller}")
        raise HTTPException(status_code=403, detail=f"Access denied from {caller}")

    return {"tenant_id": "admin", "api_key": api_key_raw, "is_admin": True}
