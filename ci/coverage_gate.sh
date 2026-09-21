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
MIN_COLLECTED="${MIN_COLLECTED:-244}"  # A-2 smoke: repo-wide обязан собирать не меньше канона
JUNIT_OUT="${JUNIT_OUT:-/tmp/roma_junit.xml}"  # A-2: машинный источник канонических чисел
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

run_scope() {  # A-2 (N7b): область ПРОГОНА обязана быть repo-wide и наблюдаемой
  if [ "$RUN_MODE" = "narrow" ]; then printf '%s' "tests/ (narrow — негатив/локально)"; else printf '%s' "repo-wide"; fi
}
RUN_MODE="${RUN_MODE:-repo-wide}"
if [ "$RUN_MODE" = "narrow" ]; then RUN_TARGET="tests/"; else RUN_TARGET="."; fi

rm -f "$JSON_OUT" "$JUNIT_OUT"
"$PY" -m pytest $RUN_TARGET -q -p no:cacheprovider -p no:warnings \
  --cov=. --cov-report="json:$JSON_OUT" --junitxml="$JUNIT_OUT" >"$RUN_LOG" 2>&1
PYTEST_STATUS=$?

# A-2: число тестов из МАШИННОГО источника (junitxml), не из прогресс-строки
if [ -s "$JUNIT_OUT" ]; then
  JUNIT_SHA="$(sha256sum "$JUNIT_OUT" | cut -d' ' -f1)"
  "$PY" - "$JUNIT_OUT" "$MIN_COLLECTED" <<'PYEOF'
import sys, xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
s = root if root.tag == "testsuite" else root.find("testsuite")
n = int(s.get("tests") or 0)
print(f"JUNIT        : {sys.argv[1]} · tests={n} failures={s.get('failures')} "
      f"errors={s.get('errors')} skipped={s.get('skipped')}")
if n < int(sys.argv[2]):
    print(f"COVERAGE GATE: FAILED -> тестов собрано {n} < порога {sys.argv[2]} "
          f"(область прогона сузилась: ожидается repo-wide)", file=sys.stderr)
    sys.exit(9)
PYEOF
  SMOKE_STATUS=$?
  if [ "$SMOKE_STATUS" -ne 0 ]; then echo "JUNIT sha256 : $JUNIT_SHA"; exit 9; fi
  echo "JUNIT sha256 : $JUNIT_SHA"
  echo "RUN SCOPE    : $(run_scope) · target=$RUN_TARGET · collected>=$MIN_COLLECTED (smoke PASS)"
else
  fail "junitxml не создан: $JUNIT_OUT (канонические числа обязаны быть машинными — A-2)"
fi

if [ ! -s "$JSON_OUT" ]; then
  echo "pytest exit: $PYTEST_STATUS"
  fail "no coverage report at $JSON_OUT (pytest exit $PYTEST_STATUS; see $RUN_LOG)"
fi

THRESHOLD_SHA_FULL="$(sha256sum "$THRESHOLDS" | cut -d' ' -f1)"
"$PY" - "$JSON_OUT" "$GLOBAL_FLOOR" "$MONEY_FLOOR" "$SCOPE_JSON" "$SCOPE_MODE" "$(run_scope)" "$THRESHOLD_SHA_FULL" <<'PYEOF'
import fnmatch, json, sys

path, global_floor, money_floor = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
scope_path, mode = sys.argv[4], sys.argv[5]
RUN_SCOPE_NOTE = sys.argv[6] if len(sys.argv) > 6 else "?"
THRESHOLD_SHA = sys.argv[7] if len(sys.argv) > 7 else "?"
d = json.load(open(path))
files = d["files"]
thr = json.load(open(scope_path))

def matched(name, prefixes):
    """Совпадение пути с элементом области: точное, по каталогу или по glob (fnmatch).

    A-1 (N7a): элемент вида '**/tests/**' обязан матчить и ВЛОЖЕННЫЕ тесты, а не только корень.
    '*'/'**'/'test_*.py' — тоже glob. Без этого exclude 'tests/' закрывал лишь корневой каталог,
    вложенные тесты попадали в область измерения, и TOTAL зависел от структуры каталогов.
    """
    for p in prefixes:
        if not p:
            continue
        if p in ("*", "."):
            return True
        if fnmatch.fnmatch(name, p):
            return True
        if name == p or name.startswith(p.rstrip("/") + "/"):
            return True
        if any(ch in p for ch in "*?["):
            if fnmatch.fnmatch(name, p) or fnmatch.fnmatch(name, p.rstrip("/") + "/*") or fnmatch.fnmatch(name, "/" + name):
                return True
        if p.endswith(".py") and name == p:
            return True
    return False


def is_test_path(name):
    """Тест — это путь с каталогом tests/ на ЛЮБОМ уровне или файл test_*.py / *_test.py."""
    parts = name.split("/")
    if any(seg == "tests" for seg in parts[:-1]):
        return True
    base = parts[-1]
    return base.startswith("test_") or base.endswith("_test.py") or base == "conftest.py"

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
hist = thr.get("scope_history") or []
print("=== MEASUREMENT SCOPE (A-5: три области одним блоком) ===")
print(f"COVERAGE SCOPE : product-only · {len(include)} entries · exclude={exclude or '[]'}")
print(f"             include: {shown[:150]}{' …' if len(shown) > 150 else ''}")
print(f"             exclude: {', '.join(exclude) if exclude else '[]'}")
print(f"RUN SCOPE     : repo-wide ({RUN_SCOPE_NOTE})")
print(f"THRESHOLDS    : {scope_path}")
print(f"             sha256={THRESHOLD_SHA}")
print(f"SCOPE HISTORY : {len(hist)} записей · последняя {hist[-1].get('date', '?')} "
      f"(scope: {hist[-1].get('scope') or hist[-1].get('total_pct', '?')})" if hist else "SCOPE HISTORY : 0 записей")
print("=" * 56)
print(f"TOTAL      : {prod_pct:.2f}% ({p_c}/{p_s}) floor {global_floor:.1f}   [scope: product-only]")
print(f"TOTAL ref  : {whole_pct:.2f}% ({w_c}/{w_s})            [scope: whole-repo, справочно]")
print(f"MONEY PATH : {m_pct:.2f}% ({m_c}/{m_s}) floor {money_floor:.1f}")

for p, k in sorted(((v["summary"]["percent_covered"], k) for k, v in money.items()))[:5]:
    print(f"   money lowest: {p:5.1f}%  {k}")

problems = []
for entry in include:
    if not any(matched(f, [entry]) for f in files):
        problems.append(f"scope entry matched 0 files: '{entry}' (опечатка в области измерения?)")
# Дрейф области: тесты НИКОГДА не являются продуктом. Проверка идёт по ИТОГОВОЙ области
# (include ∧ ¬exclude): при SCOPE_MODE=whole exclude пуст → тест-файлы в области → негатив срабатывает.
drifted = sorted(f for f in product if is_test_path(f))
if drifted:
    problems.append(
        f"scope drift: тест-файлы внутри области измерения (в т.ч. вложенные) — {len(drifted)} файлов, "
        f"напр. {drifted[0]} — покрытие тестов самих себя не считается покрытием продукта")
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
