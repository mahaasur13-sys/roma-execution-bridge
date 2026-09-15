#!/usr/bin/env python3
"""scripts/reconciliation.py — read-only сверка SUM(CREDIT-DEBIT) vs balance + Prometheus gauges"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from billing.pg_ledger import PGBillingLedger
from monitoring.metrics import (
    roma_ledger_computed_balance,
    roma_api_balance,
    roma_ledger_reconciliation_diff,
    roma_ledger_entry_count,
)

def main():
    ledger = PGBillingLedger()
    tenants = ["t-test-paper-20260909"]
    has_diff = False
    for tenant_id in tenants:
        try:
            balance = ledger.get_balance(tenant_id)
            computed = ledger.get_tenant_balance(tenant_id)
            count = ledger.get_tenant_entry_count(tenant_id)
            diff = abs(balance - computed)
            print(f"tenant={tenant_id} balance={balance:.6f} computed={computed:.6f} diff={diff:.9f} count={count}")
            roma_ledger_computed_balance.labels(tenant_id=tenant_id).set(computed)
            roma_api_balance.labels(tenant_id=tenant_id).set(balance)
            roma_ledger_reconciliation_diff.labels(tenant_id=tenant_id).set(diff)
            roma_ledger_entry_count.labels(tenant_id=tenant_id).set(count)
            if diff > 0.000001:
                print(f"ERROR: diff {diff} for {tenant_id}", file=sys.stderr)
                has_diff = True
        except Exception as e:
            print(f"ERROR tenant={tenant_id} {e}", file=sys.stderr)
            has_diff = True
    sys.exit(1 if has_diff else 0)

if __name__ == "__main__":
    main()
