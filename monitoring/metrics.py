"""ROMA Execution Bridge — Prometheus billing metrics + reconciliation + alerts."""

from prometheus_client import Counter, Gauge, Histogram

# --- OLD NAMES (совместимость с main.py:48) ---
gpu_seconds_total = Counter(
    "roma_gpu_seconds_total",
    "Total GPU seconds consumed",
    ["tenant_id", "plan"],
)
tokens_total = Counter(
    "roma_tokens_total",
    "Total tokens processed",
    ["tenant_id", "plan", "direction"],
)
billing_cost_total = Counter(
    "roma_billing_cost_total",
    "Total cost in USD",
    ["tenant_id", "plan", "cost_type"],
)
spend_cap_balance = Gauge(
    "roma_spend_cap_balance_usd",
    "Current balance for spend-cap tenants",
    ["tenant_id"],
)
spend_cap_blocked_total = Counter(
    "roma_spend_cap_blocked_total",
    "Spend cap blocked",
    ["tenant_id"],
)
job_cost = Histogram(
    "roma_job_cost",
    "Job cost",
    ["tenant_id"],
)

# --- NEW NAMES with roma_ prefix (алиасы для совместимости) ---
roma_spend_cap_balance_usd = spend_cap_balance
roma_gpu_seconds_total = gpu_seconds_total
roma_tokens_total = tokens_total
roma_billing_cost_total = billing_cost_total
roma_job_cost = job_cost
roma_spend_cap_blocked_total = spend_cap_blocked_total

# --- NEW — reconciliation-cron + alerts (P1b) ---
roma_ledger_computed_balance = Gauge("roma_ledger_computed_balance", "SUM(CREDIT-DEBIT)", ["tenant_id"])
roma_api_balance = Gauge("roma_api_balance", "API balance", ["tenant_id"])
roma_ledger_reconciliation_diff = Gauge("roma_ledger_reconciliation_diff", "abs(computed-api)", ["tenant_id"])
roma_ledger_entry_count = Gauge("roma_ledger_entry_count", "COUNT(*)", ["tenant_id"])
roma_tenant_balance = Gauge("roma_tenant_balance", "Current balance", ["tenant_id"])
roma_debit_total = Counter("roma_debit_total", "Debit results", ["tenant_id", "status"])
roma_debit_no_funds_total = Counter("roma_debit_no_funds_total", "No funds", ["tenant_id"])
roma_debit_amount = Histogram("roma_debit_amount", "Debit amount", ["tenant_id"])
roma_tenant_lookup_total = Counter("roma_tenant_lookup_total", "Lookup method", ["method"])
