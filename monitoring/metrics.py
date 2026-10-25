"""ROMA Execution Bridge — Prometheus billing metrics.

Instrumentation points:
- _increment_usage() → track_billing()
- _check_spend_cap() → track_spend_cap() / track_spend_cap_blocked()
- submit_job / complete_job → automatically covered via _increment_usage
"""

import logging

from prometheus_client import Counter, Gauge, Histogram

logger = logging.getLogger("roma.monitoring.metrics")

gpu_seconds_total = Counter(
    "roma_gpu_seconds_total",
    "Total GPU seconds consumed",
    ["tenant_id", "plan"],
)

tokens_total = Counter(
    "roma_tokens_total",
    "Total tokens processed (input + output)",
    ["tenant_id", "plan", "direction"],
)

billing_cost_total = Counter(
    "roma_billing_cost_total",
    "Total cost in USD",
    ["tenant_id", "plan", "cost_type"],
)

spend_cap_balance = Gauge(
    "roma_spend_cap_balance_usd",
    "Current balance (total debited) for spend-cap tenants",
    ["tenant_id", "plan"],
)

spend_cap_pct = Gauge(
    "roma_spend_cap_pct",
    "Spend-cap usage percentage (0-100+)",
    ["tenant_id", "plan"],
)

spend_cap_blocked_total = Counter(
    "roma_spend_cap_blocked_total",
    "Total jobs blocked by spend-cap exceeded",
    ["tenant_id", "plan"],
)

job_cost = Histogram(
    "roma_job_cost_usd",
    "Per-job cost distribution in USD",
    ["tenant_id", "plan"],
    buckets=(0.000001, 0.00001, 0.0001, 0.001, 0.01, 0.1, 1, 10),
)


def track_billing(
    tenant_id: str,
    plan_name: str,
    gpu_sec: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_cost: float = 0.0,
    job_id: str | None = None,
) -> None:
    """Record all billing metrics from _increment_usage."""
    if gpu_sec > 0:
        gpu_seconds_total.labels(tenant_id=tenant_id, plan=plan_name).inc(gpu_sec)
        billing_cost_total.labels(tenant_id=tenant_id, plan=plan_name, cost_type="gpu").inc(round(gpu_sec * 0.00001, 8))
    if input_tokens > 0:
        tokens_total.labels(tenant_id=tenant_id, plan=plan_name, direction="input").inc(input_tokens)
        billing_cost_total.labels(tenant_id=tenant_id, plan=plan_name, cost_type="tokens").inc(
            round(input_tokens * 0.000001, 8)
        )
    if output_tokens > 0:
        tokens_total.labels(tenant_id=tenant_id, plan=plan_name, direction="output").inc(output_tokens)
        billing_cost_total.labels(tenant_id=tenant_id, plan=plan_name, cost_type="tokens").inc(
            round(output_tokens * 0.000002, 8)
        )
    if total_cost > 0:
        job_cost.labels(tenant_id=tenant_id, plan=plan_name).observe(total_cost)


def track_spend_cap(tenant_id: str, plan_name: str, balance: float, cap: float) -> None:
    """Update spend-cap gauges from _check_spend_cap."""
    if cap <= 0:
        return
    spend_cap_balance.labels(tenant_id=tenant_id, plan=plan_name).set(balance)
    pct = (balance / cap) * 100
    spend_cap_pct.labels(tenant_id=tenant_id, plan=plan_name).set(pct)


def track_spend_cap_blocked(tenant_id: str, plan_name: str) -> None:
    """Increment blocked-job counter from _check_spend_cap."""
    spend_cap_blocked_total.labels(tenant_id=tenant_id, plan=plan_name).inc()


# --- GATE_UNAVAILABLE — гейт недоступен, исполнение блокируется (fail-closed) ---
gate_unavailable_total = Counter(
    "roma_gate_unavailable_total",
    "Execution blocked because the decision gate is unavailable (fail-closed)",
    ["scope"],
)


def track_gate_unavailable(scope: str) -> None:
    """Счётчик fail-closed отказа: авария гейта обязана быть видна снаружи, не молчать."""
    gate_unavailable_total.labels(scope=scope).inc()


# --- NEW — reconciliation-cron + alerts (P1b) — добавлено в 266283e FIX ---
roma_ledger_computed_balance = Gauge("roma_ledger_computed_balance", "SUM(DEBIT) ledger_entries", ["tenant_id"])
roma_api_balance = Gauge("roma_api_balance", "SUM(cost_usd) usage_events", ["tenant_id"])
roma_ledger_reconciliation_diff = Gauge(
    "roma_ledger_reconciliation_diff", "abs(ledger_debit - usage_cost)", ["tenant_id"]
)
roma_ledger_entry_count = Gauge("roma_ledger_entry_count", "COUNT(*)", ["tenant_id"])
roma_tenant_balance = Gauge("roma_tenant_balance", "Current balance", ["tenant_id"])
roma_debit_total = Counter("roma_debit_total", "Debit results", ["tenant_id", "status"])
roma_debit_no_funds_total = Counter("roma_debit_no_funds_total", "No funds", ["tenant_id"])
roma_debit_amount = Histogram("roma_debit_amount", "Debit amount", ["tenant_id"])
roma_tenant_lookup_total = Counter("roma_tenant_lookup_total", "Lookup method", ["method"])
roma_backup_last_success_timestamp = Gauge(
    "roma_backup_last_success_timestamp", "Last successful backup epoch seconds", []
)
roma_dr_drill_last_success_timestamp = Gauge(
    "roma_dr_drill_last_success_timestamp",
    "Last successful pg_restore drill epoch seconds",
    [],
)
roma_dr_drill_rto_seconds = Gauge("roma_dr_drill_rto_seconds", "Last pg_restore drill recovery time in seconds", [])

# алиасы для нового префикса (совместимость)
roma_spend_cap_balance_usd = spend_cap_balance
roma_gpu_seconds_total = gpu_seconds_total
roma_tokens_total = tokens_total
roma_billing_cost_total = billing_cost_total
roma_job_cost = job_cost
roma_spend_cap_blocked_total = spend_cap_blocked_total
roma_gate_unavailable_total = gate_unavailable_total
# если есть spend_cap_pct в старом — алиас тоже
try:
    roma_spend_cap_pct = spend_cap_pct
except NameError:
    pass


# --- Jev shadow (R1/R3/R5) — наблюдаемость shadow-контура ---
# Лейблы — только из фиксированных множеств ниже: ни tenant_id, ни текста сообщения.
_JEV_POINTS = ("R1", "R3", "R5")
_JEV_OUTCOMES = ("ok", "fallback")
_JEV_REASONS = ("jev_error", "disabled", "timeout", "rate_limited", "no_answer", "state_capture")
_JEV_AGREE = ("yes", "no")

jev_shadow_observations_total = Counter(
    "roma_jev_shadow_observations_total",
    "Jev shadow observations by outcome (ok = observation recorded)",
    ["point", "outcome"],
)

jev_shadow_fallback_total = Counter(
    "roma_jev_shadow_fallback_total",
    "Jev shadow fallbacks: deterministic baseline kept instead of an observation",
    ["point", "reason"],
)

jev_shadow_agreement_total = Counter(
    "roma_jev_shadow_agreement_total",
    "Jev shadow class vs deterministic verdict (compliant mapping)",
    ["point", "agree"],
)

jev_shadow_dropped_total = Counter(
    "roma_jev_shadow_dropped_total",
    "Jev shadow observations dropped by the bounded observer queue",
    ["point"],
)

jev_shadow_latency_seconds = Histogram(
    "roma_jev_shadow_latency_seconds",
    "Jev shadow observation latency (one Jev call)",
    ["point"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8),
)


def _one_of(value: str, allowed: tuple[str, ...]) -> str:
    """Неизвестное значение схлопывается в ``other`` — кардинальность лейбла фиксирована."""
    return value if value in allowed else "other"


def track_jev_shadow(
    point: str,
    *,
    outcome: str | None = None,
    reason: str | None = None,
    agree: str | None = None,
    dropped: bool = False,
    latency_s: float | None = None,
) -> None:
    """Записать метрики shadow-контура (best-effort).

    Никогда не бросает: сбой телеметрии не имеет права влиять на вердикт.
    """
    try:
        p = _one_of(point, _JEV_POINTS)
        if outcome is not None:
            jev_shadow_observations_total.labels(point=p, outcome=_one_of(outcome, _JEV_OUTCOMES)).inc()
        if reason is not None:
            jev_shadow_fallback_total.labels(point=p, reason=_one_of(reason, _JEV_REASONS)).inc()
        if agree is not None:
            jev_shadow_agreement_total.labels(point=p, agree=_one_of(agree, _JEV_AGREE)).inc()
        if dropped:
            jev_shadow_dropped_total.labels(point=p).inc()
        if latency_s is not None:
            jev_shadow_latency_seconds.labels(point=p).observe(max(0.0, float(latency_s)))
    except Exception as exc:  # noqa: BLE001 — телеметрия не ломает горячий путь
        logger.debug("jev shadow metrics failed: %r", exc)
