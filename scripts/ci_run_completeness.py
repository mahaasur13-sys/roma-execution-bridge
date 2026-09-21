#!/usr/bin/env python3
"""A-6: полнота набора тестов — «набор нельзя тихо сузить».

Зачем (класс дефекта, не инстанс): смоук-порог `collected >= 244` не отличал полный
набор от набора, из которого молча выпало 19 тестов: фильтр пути, `-k` или
`--deselect` оставляли гейт зелёным. Точное равенство канону закрывает это.

Три независимых числа берутся из ОДНОГО артефакта (junitxml):
  * declared — сумма атрибутов tests у <testsuite>: сколько pytest объявил собранными;
  * recorded — число элементов <testcase> в XML: сколько реально записано;
  * outcomes — passed + failures + errors + skipped, посчитанные по элементам.
Расхождение любой пары → FAIL: молчаливый deselect виден как declared < канона,
«атрибут врёт» — как declared ≠ recorded, потеря исхода — как recorded ≠ outcomes.

Канон (точные числа последнего repo-wide прогона) — `.ci/run-completeness.json`.
Отличие от канона → FAIL, пока числа не переписаны ЯВНЫМ диффом манифеста:
порог никогда не понижается сам по себе.

Режим `--mode narrow` (локальный/негативный прогон) печатает, что канон НЕ применяется,
но расхождения declared/recorded/outcomes проверяет так же — сужение области прогона
не должно быть молчаливым даже там.

CLI:
    ci_run_completeness.py --junit /tmp/roma_junit.xml
                          [--manifest .ci/run-completeness.json]
                          [--mode repo-wide|narrow]
Коды выхода: 0 — полнота подтверждена; 3 — любой отказ (fail-closed).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import xml.etree.ElementTree as ET

EXIT_FAIL = 3
REQUIRED_KEYS = ("collected", "passed", "failures", "errors", "skipped")


class CompletenessError(Exception):
    """Отказ проверки полноты: причина печатается в stderr, код выхода — 3."""


def load_manifest(path: pathlib.Path) -> tuple[dict, str]:
    if not path.exists():
        raise CompletenessError(f"манифест канона не найден: {path} (источник точных чисел набора)")
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CompletenessError(f"{type(exc).__name__} в манифесте {path}: {exc}") from exc
    canon = data.get("canon")
    if not isinstance(canon, dict):
        raise CompletenessError(f"манифест {path}: нет объекта 'canon' с точными числами")
    for key in REQUIRED_KEYS:
        value = canon.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise CompletenessError(f"манифест {path}: canon['{key}'] не целое число: {value!r}")
    return data, hashlib.sha256(raw).hexdigest()


def parse_junit(path: pathlib.Path) -> tuple[int, dict]:
    if not path.exists():
        raise CompletenessError(f"junitxml не найден: {path} (числа обязаны быть машинными)")
    if path.stat().st_size == 0:
        raise CompletenessError(f"junitxml пуст: {path} (прогон не записал результат)")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise CompletenessError(f"junitxml не читается ({type(exc).__name__}): {exc}") from exc

    suites = [root] if root.tag == "testsuite" else [s for s in root.iter("testsuite")]
    if not suites:
        raise CompletenessError(f"в junitxml нет ни одного <testsuite>: {path}")

    declared = 0
    for suite in suites:
        declared += int(suite.get("tests") or 0)

    outcomes = {"passed": 0, "failures": 0, "errors": 0, "skipped": 0}
    recorded = 0
    for suite in suites:
        for case in suite.iter("testcase"):
            recorded += 1
            if case.find("failure") is not None:
                outcomes["failures"] += 1
            elif case.find("error") is not None:
                outcomes["errors"] += 1
            elif case.find("skipped") is not None:
                outcomes["skipped"] += 1
            else:
                outcomes["passed"] += 1
    return declared, outcomes | {"recorded": recorded}


def print_block(
    *,
    junit: pathlib.Path,
    declared: int,
    observed: dict,
    mode: str,
    canon: dict,
    canon_path: pathlib.Path,
    canon_sha: str,
) -> None:
    executed = sum(observed[k] for k in ("passed", "failures", "errors", "skipped"))
    print("=== RUN COMPLETENESS (A-6: collected == executed) ===")
    print(f"JUNIT         : {junit}")
    print(f"COLLECTED     : {declared}  (объявлено pytest в junitxml)")
    print(f"EXECUTED      : {observed['recorded']}  (элементов <testcase> в том же файле)")
    print(
        f"OUTCOMES      : {observed['passed']} passed · {observed['failures']} failed · "
        f"{observed['errors']} errors · {observed['skipped']} skipped = {executed}"
    )
    print(f"CANON         : {canon_path}")
    print(f"             sha256={canon_sha}")
    print(
        f"             {canon['collected']} collected / {canon['passed']} passed + "
        f"{canon['skipped']} skipped (fact {canon.get('measured_on', '?')})"
    )
    print(f"RUN MODE      : {mode}")
    if mode == "narrow":
        print("             канон НЕ применяется (локальный/негативный прогон) — область сужена явно")
    print("=" * 56)


def check(
    *,
    junit: pathlib.Path,
    manifest: pathlib.Path,
    mode: str,
) -> int:
    canon_data, canon_sha = load_manifest(manifest)
    canon = canon_data["canon"]
    declared, observed = parse_junit(junit)
    print_block(
        junit=junit,
        declared=declared,
        observed=observed,
        mode=mode,
        canon=canon,
        canon_path=manifest,
        canon_sha=canon_sha,
    )

    problems: list[str] = []
    executed = sum(observed[k] for k in ("passed", "failures", "errors", "skipped"))
    if declared != observed["recorded"]:
        problems.append(
            f"collected != executed: junit объявляет tests={declared}, "
            f"записано элементов={observed['recorded']}"
        )
    if observed["recorded"] != executed:
        problems.append(
            f"исходы не сходятся с записями: {executed} исходов против "
            f"{observed['recorded']} элементов (потерян результат теста)"
        )
    if mode != "narrow":
        actual = {
            "collected": declared,
            "passed": observed["passed"],
            "failures": observed["failures"],
            "errors": observed["errors"],
            "skipped": observed["skipped"],
        }
        for key in REQUIRED_KEYS:
            if actual[key] != canon[key]:
                problems.append(
                    f"{key}: {actual[key]} != канон {canon[key]} "
                    f"(набор изменился — обнови {manifest.name} явным диффом)"
                )

    if problems:
        raise CompletenessError("; ".join(problems))
    print(
        "RUN COMPLETENESS: PASSED"
        + (" (канон не применялся)" if mode == "narrow" else "")
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A-6: полнота набора тестов (fail-closed)")
    parser.add_argument("--junit", default="/tmp/roma_junit.xml", help="junitxml одного прогона")
    parser.add_argument(
        "--manifest",
        default=str(pathlib.Path(__file__).resolve().parents[1] / ".ci" / "run-completeness.json"),
        help="манифест канона (точные числа последнего repo-wide прогона)",
    )
    parser.add_argument("--mode", choices=("repo-wide", "narrow"), default="repo-wide")
    args = parser.parse_args(argv)

    try:
        return check(
            junit=pathlib.Path(args.junit),
            manifest=pathlib.Path(args.manifest),
            mode=args.mode,
        )
    except CompletenessError as exc:
        print(f"RUN COMPLETENESS: FAILED -> {exc}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
