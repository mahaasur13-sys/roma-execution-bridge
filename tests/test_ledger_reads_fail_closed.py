"""Ledger reads fail-closed: пять методов чтения и /billing/ledger без in-memory fallback.

Продолжение FIX2 (аудит 55b9ed44, п.4). Там ``get_tenant_balance`` перестал считать
баланс из ``_entries`` при недоступном PG; остальные пять читающих методов
(``get_tenant_debit_sum``, ``get_tenant_usage_cost``, ``get_tenant_entries``,
``ledger_summary``, ``get_tenant_entry_count``) продолжали отдавать число/записи из
памяти процесса. Зеркало ``_entries`` не видит записей других воркеров, поэтому такое
число — не «деградированное», а неверное: сумма дебетов занижена, остаток завышен.
Контракт теперь один: PG недоступен → ``PGUnavailableError`` → маршрут 503.

Без сети и без живого PG: ``_pg_execute`` подменён, тенанты — штатный seed.
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

import db_adapter as db
import main
from billing.pg_ledger import PGBillingLedger, PGUnavailableError
from deps import register_key


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _mirror_entry(tenant_id: str) -> dict:
    """Запись, уже принятая PG и потому попавшая в зеркало: читать её при сбое нельзя."""
    return {
        "ledger_id": "led-mirror",
        "timestamp": 0,
        "tenant_id": tenant_id,
        "type": "CREDIT",
        "amount": 5.0,
        "currency": "USD",
        "metadata": {},
    }


def _pg_down(*args, **kwargs):
    raise PGUnavailableError("PG not configured or unavailable")


READ_CALLS = {
    "get_tenant_debit_sum": lambda ledger, tenant_id: ledger.get_tenant_debit_sum(tenant_id),
    "get_tenant_usage_cost": lambda ledger, tenant_id: ledger.get_tenant_usage_cost(tenant_id),
    "get_tenant_entries": lambda ledger, tenant_id: ledger.get_tenant_entries(tenant_id),
    "ledger_summary": lambda ledger, tenant_id: ledger.ledger_summary(),
    "get_tenant_entry_count": lambda ledger, tenant_id: ledger.get_tenant_entry_count(tenant_id),
}


# ── 5. Пять читающих методов: сбой PG → исключение, даже если зеркало заполнено ──


@pytest.mark.parametrize("method", sorted(READ_CALLS))
def test_read_raises_on_pg_down_even_with_filled_mirror(monkeypatch, method):
    tenant_id = _uniq("test-lfc")
    ledger = PGBillingLedger()
    ledger._entries.append(_mirror_entry(tenant_id))
    monkeypatch.setattr(ledger, "_pg_execute", _pg_down)

    with pytest.raises(PGUnavailableError):
        READ_CALLS[method](ledger, tenant_id)


# ── 6. /billing/ledger: сбой entries или count → 503, штатный путь → 200 ──────


@pytest.fixture()
def world(monkeypatch):
    """Тенант (штатный seed) + ключ; ledger подменяется в роутере по месту."""
    tenant_id = _uniq("test-lfc")
    key = _uniq("key-lfc")
    db.seed_tenants({key: {"tenant_id": tenant_id, "name": "LFC"}})
    monkeypatch.setattr(main, "is_email_verified", lambda api_key: True)
    original = dict(main.API_KEYS)
    register_key(key, {"tenant_id": tenant_id})

    async def _noop():
        return None

    monkeypatch.setattr(main, "init_worker", lambda: None)
    monkeypatch.setattr(main, "poll_and_execute", _noop)

    yield {
        "tenant_id": tenant_id,
        "key": key,
        "client": TestClient(main.app, raise_server_exceptions=False),
    }

    main.API_KEYS.clear()
    main.API_KEYS.update(original)


def _ledger_failing_on(monkeypatch, failing_op: str, tenant_id: str) -> PGBillingLedger:
    """Настоящий ledger, у которого падает ровно одна операция чтения; остальные — успешны."""
    ledger = PGBillingLedger()
    ledger._entries.append(_mirror_entry(tenant_id))

    def _execute(operation, query, params=None, fetch=False):
        if operation == failing_op:
            raise PGUnavailableError("PG not configured or unavailable")
        if operation == "ledger_entries":
            return [("led-1", tenant_id, "CREDIT", 2.5, "USD", {}, _ts())]
        if operation == "ledger_count":
            return [(1,)]
        if operation == "ledger_balance":
            return [(2.5,)]
        raise AssertionError(f"неожиданная операция: {operation}")

    monkeypatch.setattr(ledger, "_pg_execute", _execute)
    return ledger


def _ts():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


@pytest.mark.parametrize("failing_op", ("ledger_entries", "ledger_count"))
def test_billing_ledger_route_returns_503_when_read_fails(world, monkeypatch, failing_op):
    import routers.billing as billing_router

    monkeypatch.setattr(
        billing_router, "billing_ledger", _ledger_failing_on(monkeypatch, failing_op, world["tenant_id"])
    )
    resp = world["client"].get("/billing/ledger", headers={"X-API-Key": world["key"]})
    assert resp.status_code == 503, resp.text


def test_billing_ledger_route_ok_when_pg_reads_succeed(world, monkeypatch):
    import routers.billing as billing_router

    monkeypatch.setattr(billing_router, "billing_ledger", _ledger_failing_on(monkeypatch, "no-op", world["tenant_id"]))
    resp = world["client"].get("/billing/ledger", headers={"X-API-Key": world["key"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [e["amount"] for e in body["entries"]] == [2.5]
    assert body["total_entries"] == 1
