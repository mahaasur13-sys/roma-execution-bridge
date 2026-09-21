#!/usr/bin/env bash
# P1-D coverage ratchet gate.
#
# Замер 2026-09-21 (до гейта):
#   TOTAL        32.0%  (5627/17560 stmts)
#   money path   48.1%  (477/991)  — billing/* + ledger/idempotency
#   lowest: billing/metering.py, billing/invoicing.py, billing/stripe_client.py,
#           billing/aggregator.py — 0% (не покрыты ни одним тестом)
#
# Правило: порог НИКОГДА не понижается. Поднимать — только вместе с новыми тестами.
set -uo pipefail

PY="${PY:-python3}"
# Пороги — единый источник истины: .ci/coverage-thresholds.json (ratchet: только вверх).
# Env-переменные перекрывают файл (нужно для негативных прогонов гейта).
THRESHOLDS=".ci/coverage-thresholds.json"
if [ -f "$THRESHOLDS" ]; then
  FILE_GLOBAL="$(python3 -c "import json;print(json.load(open('$THRESHOLDS'))['global_floor'])")"
  FILE_MONEY="$(python3 -c "import json;print(json.load(open('$THRESHOLDS'))['money_floor'])")"
else
  FILE_GLOBAL=31; FILE_MONEY=46
fi

GLOBAL_FLOOR="${GLOBAL_FLOOR:-$FILE_GLOBAL}"
MONEY_FLOOR="${MONEY_FLOOR:-$FILE_MONEY}"
JSON_OUT="${JSON_OUT:-/tmp/roma_cov.json}"

"$PY" -m pytest tests/ -q -p no:cacheprovider -p no:warnings \
  --cov=. --cov-report="json:$JSON_OUT" >/tmp/roma_cov_run.log 2>&1
PYTEST_STATUS=$?

"$PY" - "$JSON_OUT" "$GLOBAL_FLOOR" "$MONEY_FLOOR" <<'PYEOF'
import json, sys

path, global_floor, money_floor = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
d = json.load(open(path))
files = d["files"]

def pct(sel):
    c = sum(v["summary"]["covered_lines"] for v in sel.values())
    s = sum(v["summary"]["num_statements"] for v in sel.values())
    return (100.0 * c / s if s else 0.0), c, s

g_top, g_c, g_s = (d["totals"]["percent_covered"], d["totals"]["covered_lines"],
                   d["totals"]["num_statements"])
money = {k: v for k, v in files.items()
         if (k.startswith("billing/") or "ledger" in k.lower() or "idempotenc" in k.lower())
         and not k.startswith("tests/")}
m_top, m_c, m_s = pct(money)

print(f"TOTAL      : {g_top:.1f}% ({g_c}/{g_s}) floor {global_floor:.1f}")
print(f"MONEY PATH : {m_top:.1f}% ({m_c}/{m_s}) floor {money_floor:.1f}")

worst = sorted(((v["summary"]["percent_covered"], k) for k, v in money.items()))[:5]
for p, k in worst:
    print(f"   money lowest: {p:5.1f}%  {k}")

failed = []
if g_top < global_floor:
    failed.append(f"TOTAL {g_top:.1f}% < {global_floor:.1f}%")
if m_top < money_floor:
    failed.append(f"MONEY PATH {m_top:.1f}% < {money_floor:.1f}%")

if failed:
    print("COVERAGE GATE: FAILED -> " + "; ".join(failed))
    sys.exit(1)
print("COVERAGE GATE: PASSED")
PYEOF
GATE_STATUS=$?

echo "pytest exit: $PYTEST_STATUS"
if [ "$GATE_STATUS" -ne 0 ]; then exit 1; fi
if [ "$PYTEST_STATUS" -ne 0 ]; then exit 2; fi
exit 0
