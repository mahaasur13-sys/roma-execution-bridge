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
#
# T4 fail-closed fix (2026-09-21):
#   * путь к порогам — относительно самого скрипта (был CWD-зависимый ".ci/...":
#     при запуске вне корня репозитория гейт молча брал хардкод-фолбэк 31/46);
#   * нет файла / битый / пустой / нечисловой порог → exit 3 с внятным сообщением;
#   * в stdout печатаются ИЗМЕРЕННОЕ покрытие и ПРИМЕНЁННЫЙ порог (total + money);
#   * гейт не читает покрытие, оставшееся от предыдущего прогона.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
THRESHOLDS="$REPO_ROOT/.ci/coverage-thresholds.json"

# CWD-гигиена (A1/Ф0.4): гейт обязан работать из любого каталога, а pytest —
# видеть tests/ и конфиг корня репозитория, а не каталог вызова.
cd "$REPO_ROOT" || fail "cannot cd to repo root $REPO_ROOT"

PY="${PY:-python3}"
JSON_OUT="${JSON_OUT:-/tmp/roma_cov.json}"
RUN_LOG="${RUN_LOG:-/tmp/roma_cov_run.log}"

fail() { echo "COVERAGE GATE: FAILED -> $*" >&2; exit 3; }

read_floor() {  # $1 = json key; печатает число, при любой проблеме — exit≠0 + сообщение в stderr
  "$PY" - "$THRESHOLDS" "$1" <<'PYEOF'
import json, sys

path, key = sys.argv[1], sys.argv[2]
try:
    value = json.load(open(path))[key]
except FileNotFoundError:
    print(f"thresholds file not found: {path}", file=sys.stderr)
    sys.exit(1)
except (json.JSONDecodeError, KeyError) as exc:
    print(f"{type(exc).__name__} reading '{key}' from {path}: {exc}", file=sys.stderr)
    sys.exit(1)
except OSError as exc:
    print(f"{type(exc).__name__} reading {path}: {exc}", file=sys.stderr)
    sys.exit(1)
if isinstance(value, bool) or not isinstance(value, (int, float)):
    print(f"'{key}' is not numeric in {path}: {value!r}", file=sys.stderr)
    sys.exit(1)
print(value)
PYEOF
}

[ -f "$THRESHOLDS" ] || fail "thresholds file missing: $THRESHOLDS (ratchet source of truth)"
FILE_GLOBAL="$(read_floor global_floor)" || fail "invalid global_floor in $THRESHOLDS"
FILE_MONEY="$(read_floor money_floor)" || fail "invalid money_floor in $THRESHOLDS"

GLOBAL_FLOOR="${GLOBAL_FLOOR:-$FILE_GLOBAL}"
MONEY_FLOOR="${MONEY_FLOOR:-$FILE_MONEY}"
case "$GLOBAL_FLOOR" in ''|*[!0-9.]*) fail "global_floor is not numeric: '$GLOBAL_FLOOR'";; esac
case "$MONEY_FLOOR" in ''|*[!0-9.]*) fail "money_floor is not numeric: '$MONEY_FLOOR'";; esac

echo "thresholds : $THRESHOLDS (global $FILE_GLOBAL, money $FILE_MONEY)"
echo "applied    : total floor $GLOBAL_FLOOR, money floor $MONEY_FLOOR"

rm -f "$JSON_OUT"
"$PY" -m pytest tests/ -q -p no:cacheprovider -p no:warnings \
  --cov=. --cov-report="json:$JSON_OUT" >"$RUN_LOG" 2>&1
PYTEST_STATUS=$?

if [ ! -s "$JSON_OUT" ]; then
  echo "pytest exit: $PYTEST_STATUS"
  fail "no coverage report at $JSON_OUT (pytest exit $PYTEST_STATUS; see $RUN_LOG)"
fi

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
