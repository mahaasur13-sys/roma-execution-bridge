"""Billing routes extracted from main.py (A1 — billing cluster).

The 5 ``/billing/*`` routes, moved verbatim from ``main.py``. Paths, methods
and status codes are unchanged; the router uses ``prefix="/billing"`` so the
resulting paths match the original surface exactly. Ledger/credit formulas are
untouched.

Dependencies: ``billing_ledger`` and ``plan_config`` come from ``deps`` (shared
singletons, re-exported by ``main``). The CloudPayments client/flag are read
from ``main.*`` via a deferred import because routers/webhooks.py also reads
them through ``main`` and tests monkeypatch ``main.cloudpayments_client``.
"""

from __future__ import annotations

import uuid

import db_adapter as db
import plan_source
from fastapi import APIRouter, Depends, HTTPException, Request

from audit.event_store import write_event
from deps import (
    _admin_only,
    billing_ledger,
    limiter,
    logger,
    plan_config,
    verify_api_key,
)
from models.app import CheckoutRequest

router = APIRouter(prefix="/billing", tags=["billing"])


@limiter.limit("10/minute")
@router.post("/top-up")
async def top_up_balance(request: Request, payload: dict):
    """Admin-only manual balance credit. Requires admin API key + IP allowlist.

    Non-admin callers get 403; the credited tenant is an EXPLICIT admin-supplied
    target (never derived from the caller), and the action is audited.
    """
    admin = _admin_only(request)
    target_tenant_id = (payload.get("tenant_id") or "").strip()
    if not target_tenant_id:
        raise HTTPException(
            status_code=400, detail="tenant_id is required for admin top-up"
        )
    try:
        amount = float(payload.get("amount", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Amount must be a number")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    entry_id = billing_ledger.credit(
        target_tenant_id, amount, note=f"Admin top-up by {admin['tenant_id']}"
    )
    try:
        write_event(
            admin["tenant_id"],
            "billing.topup",
            "tenant",
            target_tenant_id,
            {"amount": amount},
        )
    except Exception as exc:
        logger.warning(
            "audit.topup_failed admin=%s target=%s: %s",
            admin["tenant_id"],
            target_tenant_id,
            exc,
        )
    return {
        "status": "ok",
        "tenant_id": target_tenant_id,
        "amount": amount,
        "entry_id": entry_id,
    }


@router.post("/create-checkout-session")
async def create_checkout_session(
    request: Request,
    body: CheckoutRequest,
    key_info: dict = Depends(verify_api_key),
):
    """
    Создаёт платёжную ссылку CloudPayments (hosted page).
    Возвращает {"url": "https://..."}.
    """
    # Deferred import: CloudPayments singletons stay in main.py (also read by
    # routers/webhooks.py via main.*), and tests monkeypatch main.cloudpayments_client.
    import main

    tenant_id = key_info.get("tenant_id", "")
    plan_name = body.plan

    # Free plan — activate immediately
    if plan_name == "free":
        db.update_tenant_subscription(tenant_id, "", "", "active", "free", None)
        return {
            "url": "",
            "plan": "free",
            "tenant_id": tenant_id,
            "message": "Free plan activated",
        }

    if not main.CLOUDPAYMENTS_ENABLED or main.cloudpayments_client is None:
        plan_example = main.CLOUDPAYMENTS_PLANS.get(plan_name, {})
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

    plan_cfg = main.CLOUDPAYMENTS_PLANS.get(plan_name)
    if not plan_cfg:
        raise HTTPException(status_code=400, detail="Invalid plan")

    email = key_info.get("email", "")

    try:
        result = main.cloudpayments_client.create_order(
            amount=plan_cfg["amount"],
            currency=plan_cfg["currency"],
            description=plan_cfg["description"],
            email=email,
            subscription_plan=plan_name,
            account_id=tenant_id,
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


def _plan_or_refusal(plan_name: str | None) -> tuple[dict | None, str | None]:
    """Лимиты тира из единственного источника (`plan_source`).

    Тир вне объявленной схемы — отказ `GATE_UNAVAILABLE` (fail-closed): молчаливый
    free-дефолт в отдаче лимитов был бы той же ложью, что и в принуждении.
    """
    try:
        return plan_config(plan_name), None
    except plan_source.PlanSourceError as exc:
        logger.error(
            "GATE_UNAVAILABLE (api.billing): %s · plan=%r — лимиты не выдаются",
            exc,
            plan_name,
        )
        return (
            None,
            f"{plan_source.GATE_UNAVAILABLE}: квота тира не установлена ({exc})",
        )


@limiter.limit("30/minute")
@router.get("/ledger")
async def get_billing_ledger(
    request: Request,
    limit: int = 20,
    key_info: dict = Depends(verify_api_key),
):
    """История списаний (дебет/кредит) для текущего tenant."""
    tenant_id = key_info["tenant_id"]
    entries = billing_ledger.get_tenant_entries(tenant_id)
    entries = entries[:limit] if limit > 0 else entries
    balance = billing_ledger.get_balance(tenant_id)
    plan_name = key_info.get("plan", "free")
    plan, refusal = _plan_or_refusal(plan_name)
    if plan is None:
        raise HTTPException(status_code=503, detail=refusal)
    return {
        "tenant_id": tenant_id,
        "plan": plan_name,
        "balance_usd": round(balance, 6),
        "spend_cap_usd": plan["spend_cap_usd"],
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
        "total_entries": billing_ledger.get_tenant_entry_count(tenant_id),
    }


@limiter.limit("30/minute")
@router.get("/spend-cap")
async def check_spend_cap_endpoint(
    request: Request,
    estimated_cost_usd: float = 0.0,
    key_info: dict = Depends(verify_api_key),
):
    """Проверить, хватит ли бюджета на задачу с указанной стоимостью."""
    tenant_id = key_info["tenant_id"]
    plan_name = key_info.get("plan", "free")
    plan, refusal = _plan_or_refusal(plan_name)
    if plan is None:
        raise HTTPException(status_code=503, detail=refusal)
    cap = plan["spend_cap_usd"]
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
        "reason": (
            ""
            if allowed
            else f"Spend cap exceeded: ${balance:.4f}/${cap:.2f} ({pct}%). Job ${estimated_cost_usd:.6f} exceeds cap."
        ),
    }


@limiter.limit("30/minute")
@router.get("/balance")
async def get_balance_endpoint(
    request: Request,
    key_info: dict = Depends(verify_api_key),
):
    """Текущий баланс, план и spend-cap тенанта (короткий ответ для AI)."""
    tenant_id = key_info["tenant_id"]
    plan_name = key_info.get("plan", "free")
    plan, refusal = _plan_or_refusal(plan_name)
    if plan is None:
        raise HTTPException(status_code=503, detail=refusal)
    cap = plan["spend_cap_usd"]
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
            "max_jobs_per_month": plan["max_jobs_per_month"],
            # G-QUOTA-AXIS-UNENFORCED: ось GPU-секунд существует в данных, но
            # принуждения по ней нет; имя поля говорит правду (per-job + производный месяц).
            "gpu_s_per_job": plan["gpu_s_per_job"],
            "gpu_s_per_month": plan["gpu_s_per_month"],
        },
    }
