"""Job billing finalize — extracted from main (A2)."""

import logging
import time

import db_adapter as db
from deps import billing_ledger, alert_dispatcher
from billing.pg_ledger import PGUnavailableError  # noqa: F401
from billing.pg_metering import PGMeteringEngine as MeteringEngine
from monitoring.metrics import (
    gpu_seconds_total,
    tokens_total,
    billing_cost_total,
    job_cost,
    roma_debit_total,
    roma_debit_no_funds_total,
    roma_debit_amount,
    roma_tenant_balance,
)
from alerts import Alert, AlertLevel

logger = logging.getLogger("roma")
metering_engine = MeteringEngine()
_burn_tracker: dict[str, list[tuple[float, float]]] = {}


def _increment_usage(
    tenant_id,
    gpu_sec=0.0,
    input_tokens=0,
    output_tokens=0,
    plan_name="free",
    job_id=None,
    backend=None,
):
    """Единая точка биллинга: условный DEBIT + запись usage_events на класс ресурса.

    Возвращает (total_cost, debited): debited=False → нехватка средств
    (fail-closed по деньгам). Поднимает PGUnavailableError, если лэджер не
    может сохранить дебет.
    """
    if not tenant_id:
        return 0.0, False

    # Q1: единственный живой потребитель event_type — get_daily_stats (фильтр
    # event_type='gpu_execution'). vastai→gpu_execution, local→cpu_execution.
    event_type = "gpu_execution" if backend == "vastai" else "cpu_execution"
    gpu_rate = 0.00001
    in_rate = 0.000001
    out_rate = 0.000002

    gpu_cost = round(gpu_sec * gpu_rate, 8) if gpu_sec > 0 else 0.0
    token_cost = (
        round(input_tokens * in_rate + output_tokens * out_rate, 8)
        if (input_tokens > 0 or output_tokens > 0)
        else 0.0
    )
    total_cost = round(gpu_cost + token_cost, 8)
    if total_cost <= 0:
        return 0.0, False

    # Один атомарный дебет на ВСЮ сумму job'а. Metadata — ПЛОСКИМИ kwargs
    # (семантика **meta): НЕ передавать вложенным metadata={...} — иначе
    # metadata->>'job_id' в БД станет NULL.
    ledger_id = billing_ledger.debit_if_funds(
        tenant_id,
        total_cost,
        idempotency_key=(f"job:{job_id}" if job_id else None),
        gpu_sec=gpu_sec,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        plan=plan_name,
        job_id=job_id,
    )
    if ledger_id is None:
        roma_debit_total.labels(tenant_id=tenant_id, status="no_funds").inc()
        roma_debit_no_funds_total.labels(tenant_id=tenant_id).inc()
        logger.warning(
            "billing.no_funds tenant=%s job=%s amount=%.8f",
            tenant_id,
            job_id,
            total_cost,
        )
        return total_cost, False

    if gpu_sec > 0:
        metering_engine.record(
            event_type=event_type,
            tenant=tenant_id,
            gpu_seconds=gpu_sec,
            job_id=job_id or "auto",
            billed=True,
            cost_usd=gpu_cost,
        )
        gpu_seconds_total.labels(tenant_id=tenant_id, plan=plan_name).inc(gpu_sec)
        billing_cost_total.labels(
            tenant_id=tenant_id, plan=plan_name, cost_type="gpu"
        ).inc(gpu_cost)

    if input_tokens > 0 or output_tokens > 0:
        # R15: фактическое число токенов как value.
        metering_engine.record(
            event_type="token_usage",
            tenant=tenant_id,
            gpu_seconds=0,
            job_id=job_id or "auto",
            billed=True,
            cost_usd=token_cost,
            value=float(input_tokens + output_tokens),
        )
        if input_tokens > 0:
            tokens_total.labels(
                tenant_id=tenant_id, plan=plan_name, direction="input"
            ).inc(input_tokens)
        if output_tokens > 0:
            tokens_total.labels(
                tenant_id=tenant_id, plan=plan_name, direction="output"
            ).inc(output_tokens)
        token_cost_input = input_tokens * in_rate
        token_cost_output = output_tokens * out_rate
        if token_cost_input > 0:
            billing_cost_total.labels(
                tenant_id=tenant_id, plan=plan_name, cost_type="tokens"
            ).inc(token_cost_input)
        if token_cost_output > 0:
            billing_cost_total.labels(
                tenant_id=tenant_id, plan=plan_name, cost_type="tokens"
            ).inc(token_cost_output)

    if total_cost > 0:
        job_cost.labels(tenant_id=tenant_id, plan=plan_name).observe(total_cost)

    # Burn-rate tracking: alert if >$1/hour over recent window
    _burn_tracker.setdefault(tenant_id, []).append((time.time(), total_cost))
    _burn_tracker[tenant_id] = [
        (t, c) for t, c in _burn_tracker[tenant_id] if time.time() - t < 3600
    ]
    recent_cost = sum(c for t, c in _burn_tracker[tenant_id] if time.time() - t < 600)
    burn_rate_hourly = recent_cost * 6 if recent_cost > 0 else 0
    if burn_rate_hourly > 1.0:
        alert_dispatcher.send(
            Alert(
                level=AlertLevel.WARNING,
                title="🔥 High GPU Burn Rate",
                body=f"Tenant `{tenant_id}` (plan `{plan_name}`) burn rate: **${burn_rate_hourly:.2f}/hour**\n"
                f"Last 10 min cost: ${recent_cost:.4f} → projected ${burn_rate_hourly:.2f}/hour\n"
                f"Threshold: $1.00/hour",
                tags={
                    "tenant_id": tenant_id,
                    "plan": plan_name,
                    "event": "high_burn_rate",
                },
            )
        )

    roma_debit_total.labels(tenant_id=tenant_id, status="ok").inc()
    roma_debit_amount.labels(tenant_id=tenant_id).observe(total_cost)
    roma_tenant_balance.labels(tenant_id=tenant_id).set(
        billing_ledger.get_tenant_balance(tenant_id)
    )

    return total_cost, True


def finalize_job_billing(
    tenant_id: str,
    job_id: str,
    gpu_sec: float,
    plan_name: str = "free",
    backend: str | None = None,
) -> str:
    """Дебет job'а в терминальном состоянии (single source of truth).

    Возвращает:
      "ok"       → дебет сохранён, usage записан;
      "no_funds" → job найден, но средств не хватает → ничего не списано;
      "skip"     → job не найден / чужой tenant / уже финализирован.
    Поднимает PGUnavailableError, если лэджер не может сохранить дебет.
    """
    job = db.get_execution_job(job_id)
    if not job or job.get("tenant_id") != tenant_id:
        return "skip"
    if job.get("status") in ("completed", "cancelled", "failed", "timeout"):
        return "skip"
    _total_cost, debited = _increment_usage(
        tenant_id, gpu_sec, plan_name=plan_name, job_id=job_id, backend=backend
    )
    return "ok" if debited else "no_funds"
