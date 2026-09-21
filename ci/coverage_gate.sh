#!/usr/bin/env bash
# P1-D coverage ratchet gate.
#
# Замер 2026-09-21:
#   product-only 24.45%  (3840/15703 stmts)   ← область гейта (floor 24.4)
#   whole-repo   32.42%  (5766/17784)         ← справочно, печатается для аудита дрейфа
#   money path   48.13%  (477/991)            — billing/* + ledger/idempotency
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
#
# Ш5 scope correction (2026-09-21):
#   * область измерения утверждается ЯВНО (scope.include/scope.exclude из файла порогов),
#     а не дефолтом --cov=. ; без scope.include гейт падает (exit 3);
#   * floor перебазирован на product-only 24.45% (тесты больше не считаются покрытием продукта);
#   * печатаются ОБА числа (whole-repo и product-only) — дрейф области видим;
#   * мета-тесты области: scope-элемент, не совпавший ни с одним файлом → FAIL;
#     tests/ внутри области измерения → FAIL (scope drift); файл вне области и вне exclude → FAIL.
#     Негатив-рычаг: SCOPE_MODE=whole → tests/ попадают в продукт → FAIL "scope drift".
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
SCOPE_JSON="${SCOPE_JSON:-$THRESHOLDS}"
SCOPE_MODE="${SCOPE_MODE:-product}"

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
echo "scope mode : $SCOPE_MODE (source: $SCOPE_JSON)"

rm -f "$JSON_OUT"
"$PY" -m pytest tests/ -q -p no:cacheprovider -p no:warnings \
  --cov=. --cov-report="json:$JSON_OUT" >"$RUN_LOG" 2>&1
PYTEST_STATUS=$?

if [ ! -s "$JSON_OUT" ]; then
  echo "pytest exit: $PYTEST_STATUS"
  fail "no coverage report at $JSON_OUT (pytest exit $PYTEST_STATUS; see $RUN_LOG)"
fi

"$PY" - "$JSON_OUT" "$GLOBAL_FLOOR" "$MONEY_FLOOR" "$SCOPE_JSON" "$SCOPE_MODE" <<'PYEOF'
import json, sys

path, global_floor, money_floor = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
scope_path, mode = sys.argv[4], sys.argv[5]
d = json.load(open(path))
files = d["files"]
thr = json.load(open(scope_path))

def matched(name, prefixes):
    """Точное совпадение пути или совпадение по каталогу (без ложно-подстрочного 'in').

    '*' и '.' — match-all: режим whole-repo нужен только как негатив на дрейф области.
    """
    for p in prefixes:
        if not p:
            continue
        if p in ("*", "."):
            return True
        if name == p or name.startswith(p.rstrip("/") + "/"):
            return True
        if p.endswith(".py") and name == p:
            return True
    return False

if mode == "whole":
    include, exclude = ["."], []
else:
    scope = thr.get("scope") or {}
    include = list(scope.get("include") or [])
    exclude = list(scope.get("exclude") or [])

if not include:
    print("SCOPE ERROR: scope.include пуст — область измерения обязана быть явной (Ш5)", file=sys.stderr)
    sys.exit(3)

def agg(sel):
    c = sum(v["summary"]["covered_lines"] for v in sel.values())
    s = sum(v["summary"]["num_statements"] for v in sel.values())
    return (100.0 * c / s if s else 0.0), c, s

product = {k: v for k, v in files.items() if matched(k, include) and not matched(k, exclude)}
prod_pct, p_c, p_s = agg(product)

whole_pct = d["totals"]["percent_covered"]
w_c, w_s = d["totals"]["covered_lines"], d["totals"]["num_statements"]

money_scope = thr.get("money_scope") or {}
m_prefixes = money_scope.get("prefixes") or ["billing/"]
m_contains = money_scope.get("name_contains") or ["ledger", "idempotenc"]
m_excl = money_scope.get("exclude_prefixes") or ["tests/"]
money = {k: v for k, v in files.items()
         if (matched(k, m_prefixes) or any(t in k.lower() for t in m_contains))
         and not matched(k, m_excl)}
m_pct, m_c, m_s = agg(money)

shown = ", ".join(include)
print(f"SCOPE      : product-only · {len(include)} entries · exclude={exclude or '[]'}")
print(f"             {shown[:150]}{' …' if len(shown) > 150 else ''}")
print(f"TOTAL      : {prod_pct:.2f}% ({p_c}/{p_s}) floor {global_floor:.1f}   [scope: product-only]")
print(f"TOTAL ref  : {whole_pct:.2f}% ({w_c}/{w_s})            [scope: whole-repo, справочно]")
print(f"MONEY PATH : {m_pct:.2f}% ({m_c}/{m_s}) floor {money_floor:.1f}")

for p, k in sorted(((v["summary"]["percent_covered"], k) for k, v in money.items()))[:5]:
    print(f"   money lowest: {p:5.1f}%  {k}")

problems = []
for entry in include:
    if not any(matched(f, [entry]) for f in files):
        problems.append(f"scope entry matched 0 files: '{entry}' (опечатка в области измерения?)")
# Дрейф области: тесты НИКОГДА не являются продуктом. Проверка не зависит от текущего
# exclude-списка (иначе негатив SCOPE_MODE=whole не срабатывает: exclude пуст → drift не виден).
TEST_PREFIX = "tests/"
drifted = sorted(f for f in files
                 if matched(f, include) and (f == TEST_PREFIX.rstrip("/") or f.startswith(TEST_PREFIX)))
if drifted:
    problems.append(
        f"scope drift: tests/ внутри области измерения ({len(drifted)} файлов, напр. {drifted[0]}) — "
        "покрытие тестов самих себя не считается покрытием продукта")
unscoped = sorted(f for f in files if not matched(f, include) and not matched(f, exclude))
if unscoped:
    problems.append(f"unscoped: файл вне области и вне exclude: {unscoped[0]}"
                    + (f" (+{len(unscoped) - 1})" if len(unscoped) > 1 else ""))
if not product:
    problems.append("scope matched 0 files total — область измерения пуста")

if problems:
    print("COVERAGE GATE: FAILED -> " + "; ".join(problems))
    sys.exit(1)

failed = []
if prod_pct < global_floor:
    failed.append(f"TOTAL {prod_pct:.2f}% < {global_floor:.1f}%")
if m_pct < money_floor:
    failed.append(f"MONEY PATH {m_pct:.2f}% < {money_floor:.1f}%")
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
