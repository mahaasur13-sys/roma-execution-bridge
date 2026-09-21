#!/usr/bin/env python3
"""A-2/N7b smoke: область прогона CI обязана быть repo-wide и собирать не меньше канона.

Зачем: 62 теста (в т.ч. saas/gateway/tests/*) не исполнялись в CI, потому что шаг вызывал
`pytest tests/`. Тесты, скрывавшие fail-open аутентификации, были спрятаны дважды: --ignore и областью прогона.
Здесь область прогона печатается явно, а число берётся из junitxml — машинного источника, а не из прогресс-строки.
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

CANON_MIN = 244


def main(argv: list[str]) -> int:
    path = Path(argv[1] if len(argv) > 1 else "junit.xml")
    if not path.exists():
        print(f"SMOKE FAIL: junitxml не найден: {path} (CI обязан запускать pytest с --junitxml)", file=sys.stderr)
        return 1
    root = ET.parse(path).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        print("SMOKE FAIL: в junitxml нет testsuite", file=sys.stderr)
        return 1
    tests = int(suite.get("tests", 0))
    counts = {k: suite.get(k) for k in ("tests", "failures", "errors", "skipped")}
    print("RUN SCOPE: repo-wide · target=. · junitxml:", counts)
    if tests < CANON_MIN:
        print(
            f"SMOKE FAIL: собрано {tests} тестов < канона {CANON_MIN} — область прогона сузилась "
            "(проверь, не вернулся ли `pytest tests/`)",
            file=sys.stderr,
        )
        return 1
    print(f"SMOKE PASS: собрано {tests} >= {CANON_MIN}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
