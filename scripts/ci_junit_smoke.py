#!/usr/bin/env python3
"""A-2/N7b smoke → A-6: обёртка над единой проверкой полноты набора.

История: здесь был СВОЙ порог (`CANON_MIN = 244`), дублирующий inline-проверку гейта.
Два порога на одну величину расходятся, а `>= 244` не отличал полный набор от набора,
из которого молча выпало 19 тестов. Реализация теперь одна —
`scripts/ci_run_completeness.py` (collected == executed + точное равенство канону
`.ci/run-completeness.json`); обёртка нужна только чтобы CI-шаг остался прежним.

CLI:
    ci_junit_smoke.py [junit.xml] [--mode repo-wide|narrow]
Код выхода: как у проверки полноты (0 — набор полон; 3 — отказ).
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "scripts" / "ci_run_completeness.py"
MANIFEST = REPO_ROOT / ".ci" / "run-completeness.json"


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    mode_args = [a for a in argv[1:] if a.startswith("--")]
    junit = args[0] if args else "junit.xml"
    return subprocess.call(
        [
            sys.executable,
            str(CHECKER),
            "--junit",
            junit,
            "--manifest",
            str(MANIFEST),
            *mode_args,
        ],
        cwd=REPO_ROOT,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv))
