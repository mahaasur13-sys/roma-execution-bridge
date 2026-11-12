"""Локальный uvicorn для e2e: SQLite уводится из дерева до импорта main.

PYTHONSTARTUP здесь не подходит: он не выполняется при ``python -m uvicorn``.
Патч DB_PATH делается до загрузки приложения, иначе подпроцесс создаст
``<repo>/data/roma.db``. PG_DSN должен быть пустой строкой в окружении
(не отсутствовать): ``load_env()`` использует setdefault и иначе мог бы
подхватить DSN из локального ``.env``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    raw = os.environ.get("ROMA_E2E_SQLITE", "").strip()
    if not raw:
        raise SystemExit("ROMA_E2E_SQLITE is required")
    port_raw = os.environ.get("ROMA_E2E_PORT", "").strip()
    if not port_raw:
        raise SystemExit("ROMA_E2E_PORT is required")

    db_path = Path(raw)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import db
    import db_adapter

    db.DB_PATH = db_path
    db_adapter.DB_PATH = db_path

    import uvicorn

    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=int(port_raw),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
