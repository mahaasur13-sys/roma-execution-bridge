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
        billing_cost_total.labels(tenant_id=tenant_id, plan=plan_name, cost_type="gpu").inc(
            round(gpu_sec * 0.00001, 8)
        )
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
