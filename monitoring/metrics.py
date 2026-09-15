# monitoring/metrics.py — FIXED: added reconciliation + billing alerts metrics
# GO reconciliation-cron + GO alerts authorized
from prometheus_client import Counter, Gauge, Histogram

# existing (from slice)
roma_spend_cap_balance_usd = Gauge("roma_spend_cap_balance_usd", "Spend cap balance", ["tenant_id"])
roma_gpu_seconds_total = Counter("roma_gpu_seconds_total", "GPU seconds", ["tenant_id", "backend"])
roma_tokens_total = Counter("roma_tokens_total", "Tokens", ["tenant_id"])
roma_billing_cost_total = Counter("roma_billing_cost_total", "Billing cost", ["tenant_id"])
roma_job_cost = Histogram("roma_job_cost", "Job cost", ["tenant_id"])
roma_spend_cap_blocked_total = Counter("roma_spend_cap_blocked_total", "Spend cap blocked", ["tenant_id"])

# NEW — reconciliation-cron (P1b)
roma_ledger_computed_balance = Gauge("roma_ledger_computed_balance", "SUM(CREDIT-DEBIT) computed", ["tenant_id"])
roma_api_balance = Gauge("roma_api_balance", "API balance", ["tenant_id"])
roma_ledger_reconciliation_diff = Gauge("roma_ledger_reconciliation_diff", "abs(computed - api)", ["tenant_id"])
roma_ledger_entry_count = Gauge("roma_ledger_entry_count", "COUNT(*) ledger", ["tenant_id"])

# NEW — alerts (P1b) — low balance / no_funds
roma_tenant_balance = Gauge("roma_tenant_balance", "Current tenant balance", ["tenant_id"])
roma_debit_total = Counter("roma_debit_total", "Debit results", ["tenant_id", "status"])
roma_debit_no_funds_total = Counter("roma_debit_no_funds_total", "No funds count", ["tenant_id"])
roma_debit_amount = Histogram("roma_debit_amount", "Debit amount distribution", ["tenant_id"])
roma_tenant_lookup_total = Counter("roma_tenant_lookup_total", "Tenant lookup method", ["method"])
