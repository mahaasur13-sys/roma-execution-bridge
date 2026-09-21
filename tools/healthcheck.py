#!/usr/bin/env python3
"""
Обязательный healthcheck для ROMA Execution Bridge.
Возвращает 0 при успехе.
"""

import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent.parent

    required_files = [
        root / "main.py",
        root / "static" / "index.html",
    ]

    missing = [
        str(path.relative_to(root)) for path in required_files if not path.exists()
    ]

    if missing:
        print("FAIL: отсутствуют обязательные файлы:", ", ".join(missing))
        return 1

    print("OK: healthcheck passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
