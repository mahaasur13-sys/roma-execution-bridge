"""OT-97 (S8): неопределённый COMMIT ledger → повтор находит запись по ledger_id.

Сценарий: INSERT закоммичен сервером, но клиент увидел ``PGUnavailableError``
(обрыв соединения после COMMIT). Повторный ``debit_if_funds`` с тем же
``idempotency_key`` обязан найти запись по ``ledger_id`` и не дать ни двойного
списания, ни ложного 503. Только живой локальный PG (маркер ``pg`` → env-skip
без PG_DSN).
"""

import hashlib
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from billing.pg_ledger import PGBillingLedger, PGUnavailableError  # noqa: E402

pytestmark = pytest.mark.pg  # G-CI-PG-CANON: env-skip без живого PG


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _count(ledger, ledger_id: str, tenant: str) -> int:
    rows = ledger._pg_execute(
        "s8_count",
        "SELECT COUNT(*) FROM ledger_entries WHERE ledger_id = %s AND tenant_id = %s",
        (ledger_id, tenant),
        fetch=True,
    )
    return rows[0][0]


def test_s8_indeterminate_commit_retry_finds_by_ledger_id(monkeypatch):
    ledger = PGBillingLedger()
    tenant = _uniq("test-s8")
    key = f"job:{_uniq('s8')}"
    amount = 1.0
    ledger.credit(tenant, 100.0)

    ledger_id = "led-" + hashlib.sha256(f"{tenant}:{key}".encode()).hexdigest()[:24]

    # Неопределённый COMMIT: строка уже закоммичена сервером, но первый вызов
    # debit_if_funds наблюдает PGUnavailableError.
    def ambiguous_commit(operation, statements, fetch_last=True):
        ledger._pg_execute(
            "s8_force_commit",
            """INSERT INTO ledger_entries (ledger_id, tenant_id, entry_type, amount, currency, metadata)
               VALUES (%s, %s, 'DEBIT', %s, 'USD', '{}'::jsonb)
               ON CONFLICT (ledger_id) DO NOTHING""",
            (ledger_id, tenant, amount),
        )
        raise PGUnavailableError("connection lost after commit (S8)")

    monkeypatch.setattr(ledger, "_txn", ambiguous_commit)

    rid, _ = ledger.debit_if_funds(tenant, amount, idempotency_key=key)
    assert rid == ledger_id, "повтор должен найти запись по ledger_id (не ложный 503)"
    assert _count(ledger, ledger_id, tenant) == 1

    # Повтор без вмешательства: ON CONFLICT DO NOTHING → тот же ledger_id, одна запись.
    monkeypatch.undo()
    rid2, _ = ledger.debit_if_funds(tenant, amount, idempotency_key=key)
    assert rid2 == ledger_id
    assert _count(ledger, ledger_id, tenant) == 1, "двойное списание"
