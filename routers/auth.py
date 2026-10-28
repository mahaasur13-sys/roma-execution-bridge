"""Auth routes extracted from main.py (A1 — auth cluster).

The 7 ``/auth/*`` paths plus their private helpers (OAuth callbacks, error
page) and local config (OAuth client ids, email service singleton, login page,
verification constants), moved verbatim from ``main.py``. Paths, methods and
status codes are unchanged; the router uses ``prefix="/auth"`` so the resulting
paths match the original surface exactly.

Dependencies come from ``deps`` / ``auth.*`` / ``db_adapter`` — never ``main``.
"""

from __future__ import annotations

import hashlib
import html
import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

import config
import db_adapter as db
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from auth.invites import (
    BETA_DEFAULT_SPEND_CAP,
    BETA_MODE,
    BETA_REQUIRE_INVITE,
    check_beta_capacity,
    use_invite,
    validate_invite,
)
from auth.sessions import create_session, delete_session, get_session
from auth.verification import create_verification
from deps import limiter, logger, lookup_key, register_key, revoke_key
from saas.email.service import EmailProvider as _EmailProvider, EmailService

# ── OAuth config ───────────────────────────────────────────────────────
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
OAUTH_ENABLED = bool(GOOGLE_CLIENT_ID or GITHUB_CLIENT_ID)
OAUTH_REDIRECT_BASE = os.environ.get("OAUTH_REDIRECT_BASE", config.PUBLIC_BASE_URL)

# ── Email service singleton ────────────────────────────────────────────
email_service = EmailService(
    provider=(
        _EmailProvider[os.environ.get("EMAIL_PROVIDER", "console").upper()]
        if os.environ.get("EMAIL_PROVIDER", "console").upper() in ("SMTP", "SENDGRID", "RESEND", "CONSOLE")
        else _EmailProvider.CONSOLE
    ),
    smtp_host=os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com"),
    smtp_port=int(os.environ.get("EMAIL_SMTP_PORT", "587")),
    smtp_user=os.environ.get("EMAIL_SMTP_USER", ""),
    smtp_password=os.environ.get("EMAIL_SMTP_PASSWORD", ""),
    from_email=os.environ.get("FROM_EMAIL", "beta@roma-execution-bridge.io"),
    from_name=os.environ.get("FROM_NAME", "ROMA Platform"),
    sendgrid_api_key=os.environ.get("SENDGRID_API_KEY", ""),
)

VERIFICATION_TOKEN_EXPIRY_HOURS = int(os.environ.get("VERIFICATION_TOKEN_EXPIRY_HOURS", "24"))
VERIFICATION_BASE_URL = os.environ.get("VERIFICATION_BASE_URL", config.PUBLIC_BASE_URL)
DEMO_API_KEY = "YOUR_API_KEY"  # static placeholder on the login page; demo keys are no longer issued

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
        <input type="text" name="api_key" placeholder="{{ demo_key }}" autofocus required>
        <button type="submit">Sign In</button>
    </form>
    <p style="margin-top:24px; color:#8b949e; text-align:center">— or sign in with —</p>
    <div style="display:flex; gap:12px; margin-top:16px">
        <a href="/auth/oauth/login/google" style="flex:1; text-align:center; padding:10px; background:#1a1f2e; border:1px solid #30363d; border-radius:8px; color:#e5e7eb; text-decoration:none; font-size:14px">🔵 Google</a>
        <a href="/auth/oauth/login/github" style="flex:1; text-align:center; padding:10px; background:#1a1f2e; border:1px solid #30363d; border-radius:8px; color:#e5e7eb; text-decoration:none; font-size:14px">🐙 GitHub</a>
    </div>

    <div class="hint">Test key: <code>{{ demo_key }}</code></div>
</div>
</body>
</html>"""

SIGNUP_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ROMA — Sign up</title>
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
.ok { background:rgba(35,134,54,0.12); border:1px solid #238636; border-radius:8px; padding:12px; color:#3fb950; font-size:14px; margin-bottom:16px }
.hint { font-size:12px; color:#6b7280; margin-top:16px; text-align:center }
.hint a { color:#3b82f6; text-decoration:none }
</style>
</head>
<body>
<div class="card">
    <h1>⚡ ROMA Execution Bridge</h1>
    <p>Create an account — we will email you a confirmation link.</p>
    <div id="msg" class="error" style="display:none"></div>
    <form id="signup-form">
        <input type="text" name="name" placeholder="Name" autocomplete="name">
        <input type="email" name="email" placeholder="you@example.com" value="{{ email }}" autocomplete="email" required>
        <input type="password" name="password" placeholder="Password (min 8 characters)" autocomplete="new-password" required>
        <input type="text" name="invite_code" placeholder="Invite code (beta)" value="{{ invite_code }}">
        <button type="submit">Sign Up</button>
    </form>
    <p class="hint">Already have an account? <a href="/auth/login">Sign in</a></p>
</div>
<script>
document.getElementById('signup-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.target;
    const body = {};
    for (const [key, value] of new FormData(form).entries()) body[key] = String(value).trim();
    const msg = document.getElementById('msg');
    msg.style.display = 'none';
    const resp = await fetch('/auth/signup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (resp.status === 201) {
        form.style.display = 'none';
        msg.className = 'ok';
        msg.textContent = 'Account created. Check your email to confirm your address.';
    } else {
        const data = await resp.json().catch(() => ({}));
        msg.className = 'error';
        msg.textContent = data.detail || 'Registration failed.';
    }
    msg.style.display = 'block';
});
</script>
</body>
</html>"""


router = APIRouter(prefix="/auth", tags=["auth"])


def _new_api_key() -> str:
    """Fresh tenant key — same shape as ``/auth/signup`` and ``/account``."""
    return f"roma-{str(uuid.uuid4())[:12]}"


def _key_page(email: str, tenant_id: str, new_key: str) -> str:
    """Email verified: the key exists only in this response, so no-store is mandatory."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ROMA — Email verified</title>
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
<h1>Email verified</h1>
<div class="sub">{html.escape(email)} — {html.escape(tenant_id)}</div>
<div class="card">
<code>{new_key}</code>
<p class="warn">Shown once — this page cannot be replayed. Send it as the X-API-Key header.</p>
</div>
<a class="back" href="/dashboard">→ dashboard</a>
</div>
</body>
</html>"""


@router.get("/login")
async def login_page(request: Request):
    """Show login form."""
    # If already logged in, redirect to dashboard
    session_id = request.cookies.get("session_id")
    if session_id and get_session(session_id):
        return RedirectResponse(url="/dashboard", status_code=302)
    return Response(
        content=LOGIN_PAGE.replace("{{ demo_key }}", DEMO_API_KEY),
        media_type="text/html",
    )


@limiter.limit("15/minute")
@router.post("/login")
async def login(request: Request):
    """Process login form submission."""
    form = await request.form()
    api_key = form.get("api_key", "")
    info = lookup_key(api_key)
    if not info:
        # Show login page with error
        error_html = LOGIN_PAGE.replace(
            "</form>",
            '<div class="error">Invalid API key. Try <code>{{ demo_key }}</code></div></form>',
        ).replace("{{ demo_key }}", DEMO_API_KEY)
        return Response(content=error_html, media_type="text/html", status_code=401)

    session_id = create_session(info["tenant_id"])

    resp = RedirectResponse(url="/dashboard", status_code=302)
    resp.set_cookie(
        "session_id",
        session_id,
        httponly=True,
        max_age=3600,
        samesite="lax",
    )
    return resp


@router.get("/logout")
async def logout(request: Request):
    """Clear session and redirect to login."""
    session_id = request.cookies.get("session_id")
    if session_id:
        delete_session(session_id)
    resp = RedirectResponse(url="/auth/login", status_code=302)
    resp.delete_cookie("session_id")
    return resp


@router.get("/signup")
async def signup_page(request: Request):
    """Signup form; ``invite`` and ``email`` come straight from the invitation link."""
    page = SIGNUP_PAGE.replace("{{ email }}", html.escape(request.query_params.get("email", ""))).replace(
        "{{ invite_code }}", html.escape(request.query_params.get("invite", ""))
    )
    return Response(
        content=page,
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/signup", status_code=201)
async def signup(payload: dict, request: Request):
    """Register new user with email/password. Sends verification email."""
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    name = payload.get("name") or email.split("@")[0]
    plan = payload.get("plan", "free")

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    # Beta capacity check
    if BETA_MODE:
        cap = check_beta_capacity()
        if not cap["allowed"]:
            raise HTTPException(
                status_code=423,
                detail="Beta is currently full. Slots: %d/%d" % (cap["current_users"], cap["max_users"]),
            )

    # Invite code validation (if required)
    invite_code = payload.get("invite_code")
    if BETA_MODE and BETA_REQUIRE_INVITE:
        if not invite_code:
            raise HTTPException(status_code=400, detail="Invite code is required for beta access")
        inv = validate_invite(invite_code)
        if inv is None:
            raise HTTPException(status_code=400, detail="Invalid or expired invite code")

    existing = db.get_user_by_email(email)
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    user_id = str(uuid.uuid4())
    tenant_id = f"tenant-{str(uuid.uuid4())[:8]}"
    api_key = _new_api_key()
    password_hash = hashlib.sha256(password.encode() + user_id.encode()).hexdigest()

    from auth.verification import generate_token

    token = generate_token()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=VERIFICATION_TOKEN_EXPIRY_HOURS)).isoformat()
    verification_url = f"{VERIFICATION_BASE_URL}/auth/verify-email?token={token}"

    db.seed_tenants({api_key: {"tenant_id": tenant_id, "plan": plan}})
    register_key(api_key, {"tenant_id": tenant_id, "plan": plan})
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

    # Mark invite as used
    if invite_code:
        use_invite(invite_code, user_id)

    # Apply beta spend-cap
    if BETA_MODE and BETA_DEFAULT_SPEND_CAP > 0:
        try:
            from billing.pg_ledger import _ensure_pool

            conn = _ensure_pool()
            if conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO tenant_spend_caps (tenant_id, max_spend_usd) VALUES (%s, %s) "
                    "ON CONFLICT (tenant_id) DO NOTHING",
                    (tenant_id, BETA_DEFAULT_SPEND_CAP),
                )
                conn.commit()
                from billing.pg_connection import _return_conn

                _return_conn(conn)
        except Exception as e:
            logger.warning("beta_spend_cap_failed", extra={"tenant_id": tenant_id, "error": str(e)})

    return {
        "status": "pending",
        "message": "Account created. Please check your email to verify your address.",
        "tenant_id": tenant_id,
    }


@router.get("/verify-email")
async def verify_email_endpoint(token: str):
    """Verify the address and hand the tenant its API key — once.

    The key is minted here, not at signup: until the address is confirmed nobody
    has proven they own the mailbox the account was created with, so the
    credential is created at the one moment it can actually be handed over. The
    plaintext lives only in this response (``no-store``); the database keeps the
    digest. The token is burned by the same UPDATE that flips ``email_verified``
    (single use and expiry are its predicates), so a replay matches no row and
    never reaches the key.
    """
    consumed = db.consume_verification_token(token)
    if not consumed:
        raise HTTPException(status_code=400, detail="Invalid or expired verification token")

    user_id = consumed["user_id"]
    tenant_id = consumed["tenant_id"]
    email = consumed["email"]

    new_key = _new_api_key()
    try:
        rotated = db.rotate_user_api_key(user_id, tenant_id, new_key)
    except Exception:
        # Ротация не прошла — возвращаем токен: иначе аккаунт остался бы
        # подтверждённым, но без ключа и без пути назад из письма.
        try:
            db.restore_verification_token(user_id, token, consumed.get("expires_at"))
        except Exception:
            logger.exception("verification_token_restore_failed user_id=%s", user_id)
        logger.exception("api_key_bootstrap_failed user_id=%s", user_id)
        raise HTTPException(status_code=500, detail="Verification failed")

    old_digest = (rotated or {}).get("old_api_key_hash") or ""
    if old_digest:
        revoke_key(old_digest)
    tenant = db.get_tenant(tenant_id) or {}
    register_key(new_key, {"tenant_id": tenant_id, "plan": tenant.get("plan", "free")})
    logger.info("api_key_bootstrapped tenant_id=%s user_id=%s", tenant_id, user_id)

    session_id = create_session(tenant_id)
    resp = HTMLResponse(
        content=_key_page(email, tenant_id, new_key),
        headers={"Cache-Control": "no-store"},
    )
    resp.set_cookie("session_id", session_id, httponly=True, max_age=3600, samesite="lax")
    return resp


@router.post("/resend-verification")
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

    return {
        "status": "sent",
        "message": "Verification email resent. Please check your inbox.",
    }


@router.get("/oauth/login/{provider}")
async def oauth_login(provider: str):
    """Redirect to Google or GitHub OAuth authorization page."""
    if not OAUTH_ENABLED:
        return Response(
            content=_error_page(
                "OAuth is not configured. Add GOOGLE_CLIENT_ID or GITHUB_CLIENT_ID to .env<br>"
                "See <a href='https://github.com/mahaasur13-sys/roma-execution-bridge/blob/master/docs/oauth-setup.md'>docs/oauth-setup.md</a>"
            ),
            media_type="text/html",
            status_code=503,
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
        media_type="text/html",
        status_code=400,
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
@router.get("/oauth/callback/{provider}")
async def oauth_callback(provider: str, code: str = "", error: str = "", request: Request = None):
    """Handle OAuth callback — exchange code, create/update user, start session."""
    if error:
        return Response(
            content=_error_page(f"OAuth authorization denied: {error}"),
            media_type="text/html",
            status_code=400,
        )
    if not code:
        return Response(
            content=_error_page("No authorization code received from OAuth provider."),
            media_type="text/html",
            status_code=400,
        )
    if not OAUTH_ENABLED:
        return Response(
            content=_error_page("OAuth is not configured."),
            media_type="text/html",
            status_code=503,
        )

    try:
        if provider == "google":
            user_info = await _oauth_google_callback(code)
        elif provider == "github":
            user_info = await _oauth_github_callback(code)
        else:
            return Response(
                content=_error_page(f"Unknown OAuth provider: {provider}"),
                media_type="text/html",
                status_code=400,
            )
    except Exception as e:
        logger.error(f"OAuth callback error ({provider}): {e}")
        return Response(
            content=_error_page(f"OAuth login failed: {str(e)}"),
            media_type="text/html",
            status_code=500,
        )

    user_id = user_info["id"]
    email = user_info["email"]
    name = user_info.get("name", email)
    prov = user_info["provider"]

    # Upsert user — if exists, reuse; otherwise create new tenant + API key
    existing = db.get_user_by_email(email)
    # Held only for a brand-new account: OAuth has no mailbox to confirm, so the
    # key is minted and handed over in this one response.
    new_api_key = ""
    if existing:
        tenant_id = existing["tenant_id"]
        logger.info(f"OAuth login: existing user {email} → tenant={tenant_id}")
    else:
        tenant_id = f"tenant-{str(uuid.uuid4())[:8]}"
        api_key = _new_api_key()
        # Tenant row first: users.tenant_id references tenants.id, and inserting
        # the user first left the FK unsatisfied on sqlite (PR-F2d). A half-created
        # account must not hand out a key or a session: without both rows the key
        # would be unrecoverable and the tenant unauthenticated, so the callback
        # fails closed instead of showing a credential nobody can use.
        try:
            db.seed_tenants({api_key: {"tenant_id": tenant_id, "plan": "free"}})
            db.upsert_oauth_user(user_id, email, name, prov, tenant_id, api_key)
        except Exception as e:
            logger.error("oauth_signup_failed provider=%s error=%s", prov, type(e).__name__)
            return Response(
                content=_error_page("Could not create the account. No key was issued — try again."),
                media_type="text/html",
                status_code=500,
                headers={"Cache-Control": "no-store"},
            )
        # Add to in-memory API key registry
        register_key(
            api_key,
            {
                "tenant_id": tenant_id,
                "plan": "free",
                "subscription_status": "active",
            },
        )
        logger.info(f"OAuth login: NEW user {email} → tenant={tenant_id}")
        new_api_key = api_key

    session_id = create_session(tenant_id)
    if new_api_key:
        resp = HTMLResponse(
            content=_key_page(email, tenant_id, new_api_key),
            headers={"Cache-Control": "no-store"},
        )
    else:
        resp = RedirectResponse(url="/dashboard", status_code=302)
    resp.set_cookie("session_id", session_id, httponly=True, max_age=3600, samesite="lax")
    return resp


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
