"""Account self-service (PR-F2c): the tenant's API key can be rotated.

The key is the only credential a tenant has, so rotation is gated on the browser
session that already authenticated it, plus one more factor that depends on how
the account was created:

  * ``provider=email`` — the account password, re-verified against the same
    sha256(password + user_id) form that ``/auth/signup`` stores;
  * OAuth (google/github) — no password exists, so the session must be younger
    than 10 minutes: a long-lived session is not proof of a present human.

A CSRF token is bound to the session id (``auth.sessions.csrf_token_for``) and
required on POST; without it the request is refused with 403.

The rotation itself is one transaction over ``users`` and ``tenants`` — see
``db_adapter.rotate_user_api_key``. The plaintext key is shown exactly once in
the response, with ``Cache-Control: no-store``; the registry in memory is
switched over only after the commit.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import time
import uuid

import db_adapter as db
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import HTMLResponse

from auth.sessions import csrf_token_for, csrf_valid, get_session
from deps import limiter, logger, register_key, revoke_key

router = APIRouter(prefix="/account", tags=["account"])

# OAuth accounts have no password to re-verify: the session itself is the proof,
# and it has to be recent enough to represent a person who is still here.
OAUTH_SESSION_MAX_AGE_S = 600


def _password_matches(user: dict, password: str) -> bool:
    """Re-check a password the way ``/auth/signup`` derived and stored it."""
    stored = user.get("password_hash") or ""
    if not stored or not password:
        return False
    candidate = hashlib.sha256(password.encode() + str(user.get("id", "")).encode()).hexdigest()
    return hmac.compare_digest(candidate, stored)


def _session(request: Request) -> tuple[str, dict]:
    session_id = request.cookies.get("session_id") or ""
    session = get_session(session_id) if session_id else None
    if not session:
        raise HTTPException(status_code=401, detail="Session required")
    return session_id, session


def _account_page(tenant_id: str, user: dict, csrf_token: str) -> str:
    provider = (user.get("provider") or "email").lower()
    second_factor = (
        '<label for="password">Password</label>'
        '<input id="password" name="password" type="password" autocomplete="current-password" required>'
        if provider == "email"
        else f'<p class="hint">Signed in via {html.escape(provider)}. For a session older than 10 minutes, '
        "sign in again before rotating.</p>"
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ROMA — Account</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background:#0f1117; color:#e5e7eb; min-height:100vh }}
.container {{ max-width:640px; margin:0 auto; padding:32px 16px }}
h1 {{ font-size:24px; font-weight:700; margin-bottom:8px }}
.sub {{ color:#9ca3af; font-size:14px; margin-bottom:24px }}
.card {{ background:#161b22; border:1px solid #30363d; border-radius:12px; padding:20px }}
dl {{ display:grid; grid-template-columns:140px 1fr; gap:8px 16px; font-size:14px; margin-bottom:20px }}
dt {{ color:#8b949e }}
label {{ display:block; font-size:13px; color:#8b949e; margin-bottom:6px }}
input {{ width:100%; padding:10px 12px; border-radius:6px; border:1px solid #30363d; background:#0d1117; color:#e5e7eb; font-size:14px }}
button {{ margin-top:16px; padding:10px 18px; border:0; border-radius:6px; background:#3b82f6; color:#fff; font-size:14px; cursor:pointer }}
.hint {{ color:#8b949e; font-size:13px }}
.warn {{ color:#f59e0b; font-size:13px; margin-top:16px }}
.back {{ display:inline-block; margin-top:20px; color:#3b82f6; font-size:14px; text-decoration:none }}
</style>
</head>
<body>
<div class="container">
<h1>Account</h1>
<div class="sub">{html.escape(tenant_id)}</div>
<div class="card">
<dl>
    <dt>Email</dt><dd>{html.escape(str(user.get('email', '')))}</dd>
    <dt>Tenant</dt><dd>{html.escape(tenant_id)}</dd>
    <dt>Sign-in method</dt><dd>{html.escape(provider)}</dd>
</dl>
<form method="post" action="/account/rotate-key">
    <input type="hidden" name="csrf_token" value="{csrf_token}">
    {second_factor}
    <button type="submit">Rotate API key</button>
</form>
<p class="warn">The current key stops working immediately. The new one is shown once — copy it before leaving the page.</p>
</div>
<a class="back" href="/dashboard">← back to dashboard</a>
</div>
</body>
</html>"""


def _rotated_page(tenant_id: str, new_key: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ROMA — New API key</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background:#0f1117; color:#e5e7eb; min-height:100vh }}
.container {{ max-width:640px; margin:0 auto; padding:32px 16px }}
h1 {{ font-size:24px; font-weight:700; margin-bottom:8px }}
.sub {{ color:#9ca3af; font-size:14px; margin-bottom:24px }}
.card {{ background:#161b22; border:1px solid #30363d; border-radius:12px; padding:20px }}
code {{ display:block; padding:14px; border-radius:8px; background:#0d1117; border:1px solid #30363d; font-family:ui-monospace, SFMono-Regular, Menlo, monospace; font-size:15px; word-break:break-all }}
.warn {{ color:#f59e0b; font-size:13px; margin-top:16px }}
.back {{ display:inline-block; margin-top:20px; color:#3b82f6; font-size:14px; text-decoration:none }}
</style>
</head>
<body>
<div class="container">
<h1>New API key</h1>
<div class="sub">{html.escape(tenant_id)}</div>
<div class="card">
<code>{new_key}</code>
<p class="warn">Shown once. The previous key no longer works. Send it as the X-API-Key header.</p>
</div>
<a class="back" href="/dashboard">← back to dashboard</a>
</div>
</body>
</html>"""


@router.get("")
async def account_page(request: Request):
    """Account form with the session-bound CSRF token."""
    session_id, session = _session(request)
    tenant_id = session["tenant_id"]
    user = db.get_user_by_tenant(tenant_id)
    if not user:
        raise HTTPException(status_code=404, detail="Account not found")
    return HTMLResponse(
        content=_account_page(tenant_id, user, csrf_token_for(session_id)),
        headers={"Cache-Control": "no-store"},
    )


@router.post("/rotate-key")
@limiter.limit("5/15minutes")
async def rotate_api_key(request: Request):
    """Rotate the tenant API key: one transaction, then the registry switch-over."""
    form = await request.form()
    csrf_token = str(form.get("csrf_token", ""))
    password = str(form.get("password", ""))
    session_id, session = _session(request)
    if not csrf_valid(session_id, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    tenant_id = session["tenant_id"]
    user = db.get_user_by_tenant(tenant_id)
    if not user:
        raise HTTPException(status_code=404, detail="Account not found")

    provider = (user.get("provider") or "").lower()
    if provider == "email":
        if not _password_matches(user, password):
            raise HTTPException(status_code=403, detail="Password verification failed")
    elif float(session.get("created_at") or 0) + OAUTH_SESSION_MAX_AGE_S < time.time():
        raise HTTPException(
            status_code=403,
            detail="Session is too old to rotate a key — sign in again",
        )

    new_key = f"roma-{str(uuid.uuid4())[:12]}"
    try:
        result = db.rotate_user_api_key(user["id"], tenant_id, new_key)
    except Exception:
        logger.exception("api_key_rotation_failed tenant_id=%s user_id=%s", tenant_id, user.get("id"))
        raise HTTPException(status_code=500, detail="Key rotation failed")

    old_api_key_hash = result.get("old_api_key_hash") or ""
    if old_api_key_hash:
        revoke_key(old_api_key_hash)

    tenant = db.get_tenant(tenant_id) or {}
    register_key(new_key, {"tenant_id": tenant_id, "plan": tenant.get("plan", "free")})
    logger.info("api_key_rotated tenant_id=%s user_id=%s", tenant_id, user.get("id"))

    return HTMLResponse(
        content=_rotated_page(tenant_id, new_key),
        headers={"Cache-Control": "no-store"},
    )
