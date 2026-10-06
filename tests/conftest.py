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
     Порядок: отказ на явно заданный боевой DSN → загрузка `.env` → пин
     тестового DSN → sweep всех db-ключей (host/dbname в лог, без креденшлов).
  4. G-CI-PG-CANON: маркер `pg` + env-skip с тройкой; в CI (CI=true) env-скипы
     запрещены — там поднимается реальный PostgreSQL.
  5. A1: пиннит ТЕСТОВЫЙ DSN (roma_test) и падает с exit=90 + маркер A1-REFUSED,
     если прогон пытается пойти против боевой БД; отдельно печатает skip-budget.
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
EXPIRY_MARK = (
    "expiry:"  # A-3b: рантайм-бюджет требует ту же тройку, что статическая политика
)
LEDGER_PATH = Path("/tmp/roma_skip_ledger.json")

# ---------------------------------------------------------------------------
# G-CI-PG-CANON: два класса скипов, различимых МАШИННО (по причине в junitxml).
#   admission — разрешённый политикой скип, не зависящий от окружения
#               (живой GPU-воркер, плейсхолдеры submit-роутера): 6 мест набора;
#   env-skip  — скип из-за ОТСУТСТВИЯ окружения (нет PG_DSN). Локально допустим
#               (0 или все PG-тесты), в CI — запрещён строго: CI обязан поднять
#               реальный PostgreSQL, иначе «зелёный» врёт о покрытии money-path.
# Тот же класс дефекта, что «ложная зелень» Г5: результат зависел от окружения,
# а не от состояния системы.
# ---------------------------------------------------------------------------
ENV_SKIP_MARKER = "env-skip:pg-unavailable"
PG_MARKER = "pg"
ENV_SKIP_REASON = (
    f"{ENV_SKIP_MARKER} — PG_DSN не задан, PG-зависимая проверка не исполнена "
    "(issue: G-CI-PG-CANON · expiry: 2026-12-31)"
)

_skips: list[dict] = []


def _refuse_foreign_db_keys(stage: str) -> None:
    """A1: отказ (exit=90), если явно заданный db-ключ ведёт не в тестовую БД."""
    import _isolation

    problems = _isolation.foreign_db_keys()
    if not problems:
        return
    message = (
        f"{_isolation.REFUSED_MARKER}: db-ключи окружения ведут не в "
        f"'{_isolation.TEST_DB_NAME}' ({stage}): " + "; ".join(problems)
    )
    print("\n" + message + "\n", flush=True)
    pytest.exit(message, returncode=_isolation.REFUSED_EXIT_CODE)


def _apply_test_isolation() -> str | None:
    """A1: зафиксировать ТЕСТОВЫЙ DSN и запретить боевой (fail-closed, exit=90)."""
    import _isolation

    try:
        dsn = _isolation.resolve_or_fail()
    except _isolation.IsolationRefused as exc:
        print("\n" + str(exc) + "\n", flush=True)
        pytest.exit(str(exc), returncode=_isolation.REFUSED_EXIT_CODE)

    if dsn:
        print(
            f"[conftest] A1 test isolation: TEST DB pinned ({_isolation.describe(dsn)})"
        )
    else:
        print(
            "[conftest] A1 test isolation: тестовый DSN не найден — "
            "PG-инварианты скипнутся (см. skip-budget ниже)"
        )
    return dsn


def _sweep_and_report() -> None:
    """A1: после пиннинга ни один db-ключ не должен указывать на боевую БД."""
    import _isolation

    findings, problems = _isolation.sweep_db_keys()
    print(
        "[conftest] A1 db-key sweep (host/dbname only): "
        + " · ".join(f"{k}={v or '<unset>'}" for k, v in findings.items())
    )
    if problems:
        message = (
            f"{_isolation.REFUSED_MARKER}: после resolve db-ключи указывают на боевую БД: "
            + "; ".join(problems)
        )
        print("\n" + message + "\n", flush=True)
        pytest.exit(message, returncode=_isolation.REFUSED_EXIT_CODE)


def pytest_sessionstart(session) -> None:
    """A1: (1) отказ на явный боевой DSN, (2) загрузка .env, (3) пин тестового DSN, (4) sweep."""
    _refuse_foreign_db_keys("до загрузки .env")

    try:
        import env_loader

        loaded = env_loader.load_env()
        if loaded:
            print(
                f"[conftest] env loaded explicitly (PG_DSN set: {bool(os.environ.get('PG_DSN'))})"
            )
    except Exception as exc:  # конфиг не критичен для unit-тестов
        print(f"[conftest] env_loader недоступен: {type(exc).__name__}: {exc}")

    _apply_test_isolation()
    _sweep_and_report()


def pytest_configure(config) -> None:
    """G-CI-PG-CANON: маркер `pg` — «тесту нужен живой PostgreSQL»."""
    config.addinivalue_line(
        "markers",
        f"{PG_MARKER}: требует живого PostgreSQL (PG_DSN); без него — env-skip",
    )


def pytest_collection_modifyitems(config, items) -> None:
    """G-CI-PG-CANON: без `PG_DSN` PG-тесты получают env-skip с полной тройкой.

    Скип ставится на этапе коллекции (а не внутри теста): он не зависит от
    порядка импортов, а маркер причины делает класс исключения машинно
    различимым — чекер канона делит скипы на admission и env.
    """
    if os.environ.get("PG_DSN"):
        return
    skip = pytest.mark.skip(reason=ENV_SKIP_REASON)
    for item in items:
        if item.get_closest_marker(PG_MARKER) is not None:
            item.add_marker(skip)


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
        if any(f in s["nodeid"] for f in INVARIANT_FILES)
        and (ISSUE_MARK not in s["reason"] or EXPIRY_MARK not in s["reason"])
    ]
    invariant_skips = [
        s for s in _skips if any(f in s["nodeid"] for f in INVARIANT_FILES)
    ]

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

    invariant_files_skipped = sorted(
        {s["nodeid"].split("::")[0] for s in invariant_skips}
    )
    print(
        "\n[conftest] skip-budget:"
        f" invariant files skipped: {len(invariant_files_skipped)}"
        f" · invariant skips: {len(invariant_skips)}"
        f" · threshold: скипы без '{ISSUE_MARK} <ID>' и '{EXPIRY_MARK} <дата>' (допустимо 0)"
        f" · violations: {len(violations)}"
    )
    if invariant_files_skipped:
        print("  файлы: " + ", ".join(invariant_files_skipped))
    for s in invariant_skips:
        print(f"  - {s['nodeid']} :: {s['reason'][:120]}")

    if violations:
        print(
            "\n[conftest] SKIP BUDGET VIOLATION: инвариантный тест скипнут без тройки issue+expiry:"
        )
        for v in violations:
            print(f"  - {v['nodeid']} :: {v['reason'][:160]}")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
