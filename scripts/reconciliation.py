#!/usr/bin/env python3
"""read-only сверка ledger↔usage по всем тенантам: агрегаты + top-20 дрейфа.

Exit 1, если есть тенанты с |diff| выше порога `ROMA_LEDGER_DRIFT_THRESHOLD_USD`
(дефолт 0.05) или сверка не удалась (PG недоступен, запрос упал).
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from billing.pg_ledger import PGBillingLedger, drift_threshold_from_env


def main() -> int:
    threshold = drift_threshold_from_env()
    try:
        summary = PGBillingLedger().reconciliation_summary(threshold)
    except Exception as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 1
    print(
        f"threshold={threshold} diff_max={summary['diff_max']:.6f} drifted={summary['drifted']} "
        f"entries={summary['entries_total']} spend_cap_exceeded={summary['spend_cap_exceeded']}"
    )
    for row in summary["drifted_top"]:
        print(f"  tenant={row.get('tenant_id')} diff={float(row.get('diff') or 0):.6f}")
    return 1 if summary["drifted"] else 0


if __name__ == "__main__":
    sys.exit(main())
