#!/usr/bin/env bash
# P1-C single-file parity gate.
#
# Ловушка, ради которой это написано: одиночный прогон файла давал не тот же
# результат, что полный прогон (PG_DSN появлялся как сайд-эффект импорта main.py).
# Гейт падает, если для любого из ключевых файлов результаты расходятся.
#
# Использование:  bash ci/parity_check.sh [file1.py file2.py ...]
set -uo pipefail

PY="${PY:-python3}"
KEY_FILES=("$@")
if [ ${#KEY_FILES[@]} -eq 0 ]; then
  KEY_FILES=(
    tests/test_ledger_append_only_trigger.py
    tests/test_ledger_atomicity.py
    tests/test_p1_submit_idempotency.py
    tests/test_p1_billing_once.py
    tests/test_quota_gate_total.py
    tests/test_p0_sqlite_rowcount.py
    tests/test_p0_security.py
  )
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

counts() {  # $1 = junit xml
  "$PY" - "$1" <<'PYEOF'
import sys, xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
suite = root if root.tag == "testsuite" else root[0]
print(suite.get("tests", "0"), suite.get("failures", "0"),
      suite.get("errors", "0"), suite.get("skipped", "0"))
PYEOF
}

echo "== full-suite run =="
"$PY" -m pytest tests/ -q -p no:cacheprovider -p no:warnings \
  --junitxml="$TMP/full.xml" >"$TMP/full.log" 2>&1
FULL_STATUS=$?
# полный прогон может упасть по причинам вне парности — фиксируем, но не сравниваем exit code

FAILED=0
for f in "${KEY_FILES[@]}"; do
  xml="$TMP/$(basename "$f").xml"
  # PARITY_SOLO_ARGS — только для негативной проверки самого гейта (напр. --noconftest)
  # shellcheck disable=SC2086
  "$PY" -m pytest "$f" -q -p no:cacheprovider -p no:warnings ${PARITY_SOLO_ARGS:-} \
    --junitxml="$xml" >"$TMP/$(basename "$f").log" 2>&1 || true
  solo="$(counts "$xml")"
  # результат того же файла в полном прогоне
  "$PY" - "$TMP/full.xml" "$f" <<'PYEOF' >"$TMP/full_one.txt"
import sys, xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
target = sys.argv[2].replace(".py", "")
tot = fail = err = skip = 0
for tc in root.iter("testcase"):
    cn = tc.get("classname", "")
    if target.split("/")[-1] in cn:
        tot += 1
        for ch in tc:
            if ch.tag == "failure": fail += 1
            elif ch.tag == "error": err += 1
            elif ch.tag == "skipped": skip += 1
print(tot, fail, err, skip)
PYEOF
  full="$(cat "$TMP/full_one.txt")"
  if [ "$solo" == "$full" ]; then
    echo "PARITY OK   $f : $solo"
  else
    echo "PARITY FAIL $f : solo=[$solo] full=[$full]"
    FAILED=1
  fi
done

echo "== skip ledger =="
if [ -f /tmp/roma_skip_ledger.json ]; then
  "$PY" -c "import json;d=json.load(open('/tmp/roma_skip_ledger.json'));print('skips:',d['skips_total'],'invariant:',d['invariant_skips'],'violations:',len(d['violations']))"
fi
echo "full-suite exit code: $FULL_STATUS"

if [ "$FAILED" -ne 0 ]; then
  echo "PARITY GATE: FAILED"
  exit 1
fi
echo "PARITY GATE: PASSED"
exit 0
