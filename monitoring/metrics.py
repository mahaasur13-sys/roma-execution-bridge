"""ROMA Execution Bridge — Prometheus billing metrics.

Instrumentation points:
- _increment_usage() → track_billing()
- _check_spend_cap() → track_spend_cap() / track_spend_cap_blocked()
- submit_job / complete_job → automatically covered via _increment_usage
"""

from prometheus_client import Counter, Gauge, Histogram

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
        billing_cost_total.labels(
            tenant_id=tenant_id, plan=plan_name, cost_type="gpu"
        ).inc(round(gpu_sec * 0.00001, 8))
    if input_tokens > 0:
        tokens_total.labels(tenant_id=tenant_id, plan=plan_name, direction="input").inc(
            input_tokens
        )
        billing_cost_total.labels(
            tenant_id=tenant_id, plan=plan_name, cost_type="tokens"
        ).inc(round(input_tokens * 0.000001, 8))
    if output_tokens > 0:
        tokens_total.labels(
            tenant_id=tenant_id, plan=plan_name, direction="output"
        ).inc(output_tokens)
        billing_cost_total.labels(
            tenant_id=tenant_id, plan=plan_name, cost_type="tokens"
        ).inc(round(output_tokens * 0.000002, 8))
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


# --- NEW — reconciliation-cron + alerts (P1b) — добавлено в 266283e FIX ---
roma_ledger_computed_balance = Gauge(
    "roma_ledger_computed_balance", "SUM(DEBIT) ledger_entries", ["tenant_id"]
)
roma_api_balance = Gauge(
    "roma_api_balance", "SUM(cost_usd) usage_events", ["tenant_id"]
)
roma_ledger_reconciliation_diff = Gauge(
    "roma_ledger_reconciliation_diff", "abs(ledger_debit - usage_cost)", ["tenant_id"]
)
roma_ledger_entry_count = Gauge("roma_ledger_entry_count", "COUNT(*)", ["tenant_id"])
roma_tenant_balance = Gauge("roma_tenant_balance", "Current balance", ["tenant_id"])
roma_debit_total = Counter("roma_debit_total", "Debit results", ["tenant_id", "status"])
roma_debit_no_funds_total = Counter(
    "roma_debit_no_funds_total", "No funds", ["tenant_id"]
)
roma_debit_amount = Histogram("roma_debit_amount", "Debit amount", ["tenant_id"])
roma_tenant_lookup_total = Counter(
    "roma_tenant_lookup_total", "Lookup method", ["method"]
)
roma_backup_last_success_timestamp = Gauge(
    "roma_backup_last_success_timestamp", "Last successful backup epoch seconds", []
)

# алиасы для нового префикса (совместимость)
roma_spend_cap_balance_usd = spend_cap_balance
roma_gpu_seconds_total = gpu_seconds_total
roma_tokens_total = tokens_total
roma_billing_cost_total = billing_cost_total
roma_job_cost = job_cost
roma_spend_cap_blocked_total = spend_cap_blocked_total
# если есть spend_cap_pct в старом — алиас тоже
try:
    roma_spend_cap_pct = spend_cap_pct
except NameError:
    pass
