"""Beta routes extracted from main.py (A1 — beta cluster).

The 5 beta routes (``/beta`` + ``/api/beta``), moved verbatim from ``main.py``.
Paths, methods and status codes are unchanged. The router uses no prefix
because the routes span two roots (``/beta`` and ``/api/beta``), so full paths
are kept in the decorators to match the original surface exactly.

Dependencies come from ``deps`` / ``auth.invites`` / ``db_adapter`` — never
``main``.
"""

from __future__ import annotations

import db_adapter as db
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import Response

from auth.invites import check_beta_capacity, validate_invite
from deps import limiter, logger, verify_api_key

router = APIRouter(tags=["beta"])

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


@router.get("/beta")
async def beta_page():
    return Response(content=BETA_FORM_HTML, media_type="text/html")


@limiter.limit("5/minute")
@router.post("/beta/apply")
async def beta_apply(request: Request, payload: dict):
    email = (payload.get("email") or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")
    company = payload.get("company", "")
    role = payload.get("role", "")
    use_case = payload.get("use_case", "")
    source = payload.get("source", "")
    lead_id = db.add_lead(email, company, role, use_case, source)
    logger.info(
        "beta_lead_created", extra={"lead_id": lead_id, "email": email[:3] + "***"}
    )
    return {
        "status": "accepted",
        "message": "Thank you! We'll reach out to you soon.",
        "lead_id": lead_id,
    }


@router.get("/beta/leads", dependencies=[Depends(verify_api_key)])
async def beta_leads(key_info: dict = Depends(verify_api_key)):
    _status = ""  # All leads
    all_leads = db.list_leads()
    return {"total": len(all_leads), "leads": all_leads}


@router.get("/api/beta/status")
async def beta_status():
    """Public endpoint: check beta status and capacity."""
    return check_beta_capacity()


@router.get("/api/beta/validate-invite")
async def beta_validate_invite(code: str):
    """Check if an invite code is valid (pre-signup)."""
    result = validate_invite(code)
    if result is None:
        raise HTTPException(status_code=404, detail="Invalid or expired invite code")
    return {
        "valid": True,
        "max_uses": result.get("invite", {}).get("max_uses", 1),
        "used_count": result.get("invite", {}).get("used_count", 0),
    }
