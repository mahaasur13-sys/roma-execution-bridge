"""Admin routes extracted from main.py (A1 — deps + admin split).

The 13 ``/admin/*`` routes plus their private helpers, moved verbatim from
``main.py``. Paths, methods and status codes are unchanged; the router uses
``prefix="/admin"`` so the resulting paths match the original surface exactly.

Dependencies come from ``deps`` (never ``main``).
"""

from __future__ import annotations

import os

import db_adapter as db
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from starlette.responses import Response

from alerts import Alert, AlertLevel
from auth.invites import check_beta_capacity, create_invite, deactivate_invite, list_invites
from deps import _admin_only, alert_dispatcher, limiter, logger, verify_api_key

router = APIRouter(prefix="/admin", tags=["admin"])


class TestAlertRequest(BaseModel):
    """Запрос на тестовую отправку алерта."""
    channel: str | None = None  # telegram, discord, email или None = все
    message: str = "🧪 Тестовый алерт ROMA Execution Bridge v2.1.0"


@limiter.limit("5/minute")
@router.post("/test-alert")
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


@router.post("/invites/create")
async def admin_create_invite(payload: dict):
    """Create a new invite code (admin only)."""
    max_uses = int(payload.get("max_uses", 1))
    note = payload.get("note", "")
    expires_hours = int(payload.get("expires_hours", 0))
    result = create_invite(max_uses=max_uses, note=note, expires_hours=expires_hours)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/invites")
async def admin_list_invites():
    """List all invite codes with usage status."""
    return {"invites": list_invites(), "beta": check_beta_capacity()}


@router.post("/invites/deactivate")
async def admin_deactivate_invite(payload: dict):
    """Deactivate an invite code."""
    code = payload.get("code", "")
    if not code:
        raise HTTPException(status_code=400, detail="Code is required")
    success = deactivate_invite(code)
    if not success:
        raise HTTPException(status_code=404, detail="Code not found")
    return {"status": "deactivated", "code": code}


@router.get("")
async def admin_page(request: Request):
    """Admin dashboard HTML page."""
    info = _admin_only(request)
    return Response(content=_render_admin_dashboard(info["tenant_id"]), media_type="text/html")


@router.get("/verification-stats")

def get_verification_stats():
    """Return verification statistics for the last 24h."""
    try:
        from db_adapter import _pg_conn, _pg_return
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT
                COUNT(*) AS total_users,
                COUNT(*) FILTER (WHERE email_verified = true) AS verified,
                COUNT(*) FILTER (WHERE email_verified = false) AS pending
            FROM users
            WHERE created_at >= now() - interval '24 hours'
        """)
        row = cur.fetchone()
        _pg_return(conn)
        total, verified, pending = row if row else (0, 0, 0)
        return {
            "period": "24h",
            "users_total": total,
            "users_verified": verified,
            "users_pending": pending,
            "verification_rate_pct": round(verified / max(total, 1) * 100, 1),
        }
    except Exception as e:
        logger.warning("verification_stats_failed", extra={"error": str(e)})
        return {"error": str(e), "period": "24h"}
async def admin_verification_stats():
    """Return email verification statistics for the last 24 hours."""
    return get_verification_stats()
@router.get("/backends", dependencies=[Depends(verify_api_key)])
async def admin_backends(key_info: dict = Depends(verify_api_key)):
    """List available execution backends and their status."""
    from backends.dispatcher import list_backends
    return {
        "active_backend": os.getenv("ROMA_EXECUTION_BACKEND", "local"),
        "backends": list_backends(),
    }


@router.get("/analytics")
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


@router.get("/analytics/users")
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


@router.get("/analytics/events")
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


@router.get("/feedback")
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
@router.get("/email-stats")
async def admin_email_stats(request: Request):
    """Get email sending statistics."""
    _admin_only(request)
    try:
        stats = db.get_email_stats()
        return {"status": "ok", "data": stats}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@limiter.limit("20/minute")
@router.post("/invite")
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

        demo_api_key = os.environ.get("ROMA_DEMO_API_KEY", "")
        invitation_link = f"https://roma-execution-bridge-asurdev.zocomputer.io/dashboard?api_key={demo_api_key}"

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
