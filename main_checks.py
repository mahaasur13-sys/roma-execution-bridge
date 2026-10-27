"""F3 checks for the ROMA startup DB path (`main._init_db_with_retry`).

Named ``main_checks.py`` (NOT ``test_*.py``) on purpose: the repo-wide canon run
(.ci/run-completeness.json, 711) must not shift. Run explicitly:

    python -m pytest main_checks.py -q

Что проверяется:
  * транзиентные отказы init_db → успех, seed_tenants при старте не вызывается;
  * отказы дольше бюджета ROMA_DB_INIT_TIMEOUT_S → последнее исключение пробрасывается;
  * бюджет читается из env (0 → первая же неудача пробрасывается, без ретраев);
  * backoff экспоненциальный и упирается в потолок (2 → 4 → 8 → 10 → 10);
  * в лог уходит только тип исключения, строка DSN не утекает.

Импорт main выполняет настоящий init_db в SQLite-режиме (PG_DSN не выставлен) — как
в остальных тестах дерева; сеть и боевая БД не затрагиваются.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

main = importlib.import_module("main")


class _Recorder:
    """Заменяет main.logger: пишем форматированные строки в список (logger.propagate=False)."""

    def __init__(self) -> None:
        self.records: list[str] = []

    def warning(self, fmt: str, *args) -> None:
        self.records.append(fmt % args)

    def error(self, fmt: str, *args) -> None:
        self.records.append(fmt % args)


@pytest.fixture
def no_sleep(monkeypatch):
    """Ретрай без реальных пауз."""
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)


def _fail_with(exc: Exception):
    def _boom():
        raise exc

    return _boom


def test_succeeds_after_transient_failures(monkeypatch, no_sleep) -> None:
    calls = {"init": 0}

    def flaky_init() -> None:
        calls["init"] += 1
        if calls["init"] < 3:
            raise RuntimeError("pg down")

    seeded: list[dict] = []
    monkeypatch.setattr(main.db, "init_db", flaky_init)
    monkeypatch.setattr(main.db, "seed_tenants", lambda keys: seeded.append(keys))

    main._init_db_with_retry(timeout_s=60, delay_s=0)

    assert calls["init"] == 3
    assert seeded == [], "seed_tenants на старте не зовётся: реестр ключей не источник тенантов"


def test_seed_is_not_called_at_startup(monkeypatch, no_sleep) -> None:
    """Старт не сеет тенантов из реестра: там только digest'ы, seed принял бы их за ключи."""
    calls = {"seed": 0}

    def counting_seed(_keys) -> None:
        calls["seed"] += 1

    monkeypatch.setattr(main.db, "init_db", lambda: None)
    monkeypatch.setattr(main.db, "seed_tenants", counting_seed)

    main._init_db_with_retry(timeout_s=60, delay_s=0)

    assert calls["seed"] == 0


def test_raises_when_budget_exhausted(monkeypatch, no_sleep) -> None:
    monkeypatch.setattr(main.db, "init_db", _fail_with(RuntimeError("pg down")))

    with pytest.raises(RuntimeError):
        main._init_db_with_retry(timeout_s=0, delay_s=0)


def test_timeout_is_read_from_env(monkeypatch) -> None:
    """env ROMA_DB_INIT_TIMEOUT_S=0 → первая неудача пробрасывается без ретраев."""
    monkeypatch.setenv("ROMA_DB_INIT_TIMEOUT_S", "0")
    monkeypatch.setattr(main.db, "init_db", _fail_with(RuntimeError("pg down")))

    def _unexpected_sleep(_seconds):
        raise AssertionError("ретрай не ожидался: env-бюджет не прочитан")

    monkeypatch.setattr(main.time, "sleep", _unexpected_sleep)

    with pytest.raises(RuntimeError):
        main._init_db_with_retry()


def test_invalid_env_falls_back_to_default(monkeypatch) -> None:
    """Нечисловой env не должен превращаться в 0 (иначе ретраев не будет вовсе).

    Часы подменены: каждый вызов monotonic() двигает время на 10 с — дефолтный
    60-секундный бюджет выбирается за 6 неудачных попыток, а не за минуту реального времени.
    """
    monkeypatch.setenv("ROMA_DB_INIT_TIMEOUT_S", "bogus")
    monkeypatch.setattr(main.db, "init_db", _fail_with(RuntimeError("pg down")))

    clock = {"t": 0.0}

    def fake_monotonic() -> float:
        clock["t"] += 10.0
        return clock["t"]

    monkeypatch.setattr(main.time, "monotonic", fake_monotonic)
    slept: list[float] = []
    monkeypatch.setattr(main.time, "sleep", lambda seconds: slept.append(seconds))

    with pytest.raises(RuntimeError):
        main._init_db_with_retry(delay_s=0)

    assert len(slept) == 5, "с нечисловым env функция обязана уйти в ретраи с дефолтным бюджетом"


def test_backoff_is_exponential_and_capped(monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(main.time, "sleep", lambda seconds: slept.append(seconds))

    outcomes = iter([RuntimeError("x")] * 5 + [None])

    def flaky_init() -> None:
        outcome = next(outcomes)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(main.db, "init_db", flaky_init)
    monkeypatch.setattr(main.db, "seed_tenants", lambda keys: None)

    main._init_db_with_retry(timeout_s=600, delay_s=2.0, max_delay_s=10.0)

    assert slept == [2.0, 4.0, 8.0, 10.0, 10.0]


def test_logs_only_exception_type(monkeypatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(main, "logger", recorder)
    monkeypatch.setattr(main.db, "init_db", _fail_with(RuntimeError("PG_DSN=postgresql://user:secret@host/db")))
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError):
        main._init_db_with_retry(timeout_s=0, delay_s=0)

    text = " | ".join(recorder.records)
    assert "RuntimeError" in text
    assert "postgresql://" not in text
    assert "PG_DSN=" not in text
    assert "secret" not in text
