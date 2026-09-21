"""P1-C: единая точка подготовки окружения для тестов + запрет молчаливых skip.

Проблема (грабли Г5), которую закрывает этот файл:
  `PG_DSN` попадал в окружение только как побочный эффект импорта `main.py`.
  `conftest.py` отсутствовал, поэтому одиночный прогон интеграционного файла
  давал skip, а полный прогон — green. Это «ложная зелень»: результат зависел
  от порядка импортов, а не от состояния системы.

Что делает:
  1. Явно загружает конфиг через `env_loader.load_env()` один раз на сессию
     (не через сайд-эффект импорта).
  2. Ведёт реестр skip и ПАДАЕТ, если инвариантный тест (ledger/billing/
     idempotency/quota) скипнут без `issue: <ID>` в причине.
  3. Пишет ledger в /tmp/roma_skip_ledger.json для CI-гейта парности.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Тесты, которые не имеют права скипаться молча (§ P1-C)
INVARIANT_FILES = (
    "test_ledger_append_only_trigger.py",
    "test_ledger_atomicity.py",
    "test_p1_billing_once.py",
    "test_p1_submit_idempotency.py",
    "test_quota_gate_total.py",
    "test_p0_sqlite_rowcount.py",
    "test_p0_security.py",
)
ISSUE_MARK = "issue:"
LEDGER_PATH = Path("/tmp/roma_skip_ledger.json")

_skips: list[dict] = []


def pytest_sessionstart(session) -> None:
    """Явная загрузка конфига — до сбора тестов, независимо от порядка импортов."""
    try:
        import env_loader

        loaded = env_loader.load_env()
        if loaded:
            print(f"[conftest] env loaded explicitly (PG_DSN set: {bool(os.environ.get('PG_DSN'))})")
    except Exception as exc:  # конфиг не критичен для unit-тестов
        print(f"[conftest] env_loader недоступен: {type(exc).__name__}: {exc}")


def pytest_runtest_logreport(report) -> None:
    if report.skipped:
        reason = ""
        if isinstance(report.longrepr, tuple) and len(report.longrepr) == 3:
            reason = report.longrepr[2]
        elif report.longrepr is not None:
            reason = str(report.longrepr)
        _skips.append({"nodeid": report.nodeid, "reason": reason})


def pytest_sessionfinish(session, exitstatus) -> None:
    violations = [
        s
        for s in _skips
        if any(f in s["nodeid"] for f in INVARIANT_FILES) and ISSUE_MARK not in s["reason"]
    ]
    invariant_skips = [s for s in _skips if any(f in s["nodeid"] for f in INVARIANT_FILES)]

    ledger = {
        "skips_total": len(_skips),
        "invariant_skips": len(invariant_skips),
        "violations": violations,
        "skips": _skips,
    }
    try:
        LEDGER_PATH.write_text(json.dumps(ledger, ensure_ascii=False, indent=2))
    except Exception:
        pass

    if invariant_skips:
        print(f"\n[conftest] invariant skips: {len(invariant_skips)} (issue-tagged ok)")
        for s in invariant_skips:
            print(f"  - {s['nodeid']} :: {s['reason'][:120]}")

    if violations:
        print(
            "\n[conftest] SKIP BUDGET VIOLATION: инвариантный тест скипнут без issue-id:"
        )
        for v in violations:
            print(f"  - {v['nodeid']} :: {v['reason'][:160]}")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
