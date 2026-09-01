"""Shared runtime dependencies extracted from ``main.py`` (A1 — deps + admin).

Holds the singletons and helpers that both ``main.py`` and the extracted
``APIRouter`` modules need, so routers can import from here instead of
importing ``main`` (which would create a circular import).

Everything here is a verbatim cut-paste from ``main.py`` — no behaviour change.
``main.py`` re-imports these names (so any test that patches ``main.X`` keeps
working). There is no ``create_app`` here.
"""

from __future__ import annotations

import ipaddress
import logging
import os

import db_adapter as db
from fastapi import Header, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from alerts import AlertDispatcher

# ── Logging ────────────────────────────────────────────────────────────
logger = logging.getLogger("roma")

# ── Rate limiting ──────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)

# ── Alerts ─────────────────────────────────────────────────────────────
alert_dispatcher = AlertDispatcher()

# ── In-memory API key registry (shared mutable dict) ──────────────────
API_KEYS: dict[str, dict] = {}

# ── Admin access ───────────────────────────────────────────────────────
ADMIN_IP_ALLOWLIST = os.environ.get(
    "ADMIN_IP_ALLOWLIST",
    "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
)


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
    tenant_id = (tenant.get("tenant_id") or "").strip()
    if not tenant_id:
        # A key that resolves to an empty/whitespace tenant is treated the same
        # as an invalid key — never a successful login.
        raise HTTPException(status_code=401, detail="Invalid API key")
    tenant["tenant_id"] = tenant_id
    tenant["api_key"] = x_api_key

    # Check email verification for API endpoints (skip auth endpoints and admin keys)
    _ADMIN_KEYS = {'admin-key-beta-2026', 'admin-super-key-xyz'}
    if x_api_key not in _ADMIN_KEYS:
        # Deferred import: is_email_verified lives in main.py (auth cluster, out
        # of scope for this split). Reading main.is_email_verified at call time
        # keeps the existing monkeypatch in tests/test_p0_security.py effective.
        import main
        verif_status = main.is_email_verified(x_api_key)
        if not verif_status:
            raise HTTPException(status_code=403, detail="Email not verified. Please verify your email first.")
    return tenant


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
