"""G-PRICING-TIER-PATH (P3 PRICING-INTEGRITY, коммит C2): событие подписки обязано
записать план в запись клиента, и только эта запись является авторитетом тарифа.

Класс дефекта (головка Stripe): ``customer.subscription.updated`` возвращало
``plan_updated``, не записывая план — единственным следствием события была нулевая
проводка. Запись клиента оставалась прежней, и все слои, читающие тариф из записи
(предиктор C1, гейт), продолжали видеть старый план: оплаченный тариф не применялся
нигде, при этом отчёт вебхука выглядел успешным.

Оси проверки:
  1) план события действительно доходит до записи клиента (позитив);
  2) replay того же события не пишет второй раз (ни записи, ни проводки);
  3) неизвестный клиент и план вне объявленной карты — отказ без записи (не silent-free);
  4) непроверенная подпись не доходит до записи вовсе.

Последняя проверка — PG-путь целиком (маркер ``pg``): реальный ``db_adapter``
пишет план в живую БД, а не в подставленную заглушку.
"""

from __future__ import annotations

import json
import uuid

import pytest

from billing.stripe_client import StripeWebhookHandler


class _FakeStripe:
    def __init__(self, signature_ok: bool = True):
        self.signature_ok = signature_ok

    def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        return self.signature_ok


class _FakeLedger:
    def __init__(self):
        self.entries: list[tuple] = []

    def credit(self, tenant_id: str, amount: float, source: str = "") -> None:
        self.entries.append(("credit", tenant_id, amount, source))

    def debit(self, tenant_id: str, amount: float, source: str = "") -> None:
        self.entries.append(("debit", tenant_id, amount, source))


class _FakeStore:
    """Запись клиента: то, что вернул бы db_adapter.get_tenant, + журнал записей."""

    def __init__(self, tenant: dict | None):
        self.tenant = tenant
        self.writes: list[dict] = []
        self.reads: list[str] = []

    def get_tenant(self, tenant_id: str) -> dict | None:
        self.reads.append(tenant_id)
        if self.tenant and self.tenant.get("id") == tenant_id:
            return dict(self.tenant)
        return None

    def update_tenant_subscription(
        self,
        tenant_id: str,
        stripe_customer_id: str = "",
        stripe_subscription_id: str = "",
        subscription_status: str = "",
        plan: str = "",
        subscription_end_date: str | None = None,
    ) -> None:
        self.writes.append(
            {
                "tenant_id": tenant_id,
                "stripe_customer_id": stripe_customer_id,
                "stripe_subscription_id": stripe_subscription_id,
                "subscription_status": subscription_status,
                "plan": plan,
            }
        )


def _tenant(tenant_id: str, plan: str = "free", status: str = "inactive") -> dict:
    return {
        "id": tenant_id,
        "plan": plan,
        "subscription_status": status,
        "stripe_customer_id": "",
        "stripe_subscription_id": "",
    }


def _payload(
    tenant_id: str,
    plan_nickname: str,
    status: str = "active",
    subscription_id: str = "sub_1",
    customer: str = "cus_1",
) -> bytes:
    return json.dumps(
        {
            "id": "evt_1",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": subscription_id,
                    "customer": customer,
                    "status": status,
                    "metadata": {"tenant_id": tenant_id},
                    "items": {"data": [{"price": {"nickname": plan_nickname}}]},
                }
            },
        }
    ).encode()


def _handler(store: _FakeStore, ledger: _FakeLedger, signature_ok: bool = True):
    return StripeWebhookHandler(_FakeStripe(signature_ok), ledger, tenant_store=store)


def test_subscription_update_writes_plan_to_tenant_record():
    """Оплаченный план доходит до записи клиента — цена дальше считается по нему."""
    store = _FakeStore(_tenant("t-pro", plan="free"))
    ledger = _FakeLedger()

    result = _handler(store, ledger).handle(_payload("t-pro", "pro"), "sig")

    assert result == {"status": "processed", "action": "plan_written", "plan": "pro"}
    assert len(store.writes) == 1, store.writes
    assert store.writes[0]["tenant_id"] == "t-pro"
    assert store.writes[0]["plan"] == "pro"
    assert store.writes[0]["subscription_status"] == "active"
    assert store.writes[0]["stripe_subscription_id"] == "sub_1"


def test_subscription_update_is_case_insensitive_on_plan():
    """'Pro' из price.nickname — тот же план: регистр не повод не применить тариф."""
    store = _FakeStore(_tenant("t-pro", plan="free"))

    result = _handler(store, _FakeLedger()).handle(_payload("t-pro", "Pro"), "sig")

    assert result["action"] == "plan_written"
    assert store.writes[0]["plan"] == "pro"


def test_replay_of_same_event_writes_once():
    """Replay: запись уже отражает событие — второй записи и второй проводки нет."""
    store = _FakeStore(_tenant("t-pro", plan="free"))
    ledger = _FakeLedger()
    handler = _handler(store, ledger)
    payload = _payload("t-pro", "pro")

    first = handler.handle(payload, "sig")
    store.tenant = _tenant("t-pro", plan="pro", status="active") | {
        "stripe_subscription_id": "sub_1"
    }
    second = handler.handle(payload, "sig")

    assert first["action"] == "plan_written"
    assert second == {"status": "processed", "action": "plan_unchanged", "plan": "pro"}
    assert len(store.writes) == 1, store.writes
    assert len(ledger.entries) == 1, ledger.entries


def test_unknown_customer_writes_nothing():
    """Клиента нет в записях — отказ отдельным статусом, а не запись «в никуда»."""
    store = _FakeStore(None)
    ledger = _FakeLedger()

    result = _handler(store, ledger).handle(_payload("t-ghost", "pro"), "sig")

    assert result["status"] == "unknown_tenant"
    assert result["action"] == "plan_not_written"
    assert store.writes == []
    assert ledger.entries == []


def test_missing_tenant_metadata_writes_nothing():
    """Событие без metadata.tenant_id не привязывается к клиенту наугад."""
    store = _FakeStore(_tenant("t-pro"))
    payload = json.dumps(
        {
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_1",
                    "status": "active",
                    "items": {"data": [{"price": {"nickname": "pro"}}]},
                }
            },
        }
    ).encode()

    result = _handler(store, _FakeLedger()).handle(payload, "sig")

    assert result["status"] == "unknown_tenant"
    assert store.reads == []
    assert store.writes == []


def test_plan_outside_declared_map_writes_nothing():
    """План вне объявленной карты — отказ, а не подстановка free-тарифа."""
    store = _FakeStore(_tenant("t-pro"))

    result = _handler(store, _FakeLedger()).handle(_payload("t-pro", "gold"), "sig")

    assert result["status"] == "unknown_plan"
    assert result["action"] == "plan_not_written"
    assert store.writes == []


def test_unverified_signature_never_touches_record():
    """Непроверенная подпись — до записи дело не доходит (SEC-1 остаётся отдельной эпохой)."""
    store = _FakeStore(_tenant("t-pro"))

    result = _handler(store, _FakeLedger(), signature_ok=False).handle(
        _payload("t-pro", "pro"), "bad-sig"
    )

    assert result == {"status": "signature_failed"}
    assert store.reads == []
    assert store.writes == []


def test_invoice_paid_keeps_crediting_tenant():
    """Соседняя ветка не задета: invoice.paid по-прежнему зачисляет на запись клиента."""
    store = _FakeStore(_tenant("t-pro"))
    ledger = _FakeLedger()
    payload = json.dumps(
        {
            "type": "invoice.paid",
            "data": {
                "object": {
                    "amount_paid": 2500,
                    "metadata": {"tenant_id": "t-pro"},
                }
            },
        }
    ).encode()

    result = _handler(store, ledger).handle(payload, "sig")

    assert result == {"status": "processed", "action": "credit_applied"}
    assert ledger.entries == [("credit", "t-pro", 25.0, "stripe_invoice_paid")]


@pytest.mark.pg  # G-CI-PG-CANON: env-skip без живого PG
def test_subscription_update_persists_plan_in_live_db():
    """PG-путь целиком: план события виден в записи клиента после чтения из БД."""
    import db_adapter as db

    tenant_id = f"test-sub-{uuid.uuid4().hex[:8]}"
    db.seed_tenants({f"key-{tenant_id}": {"tenant_id": tenant_id, "name": "sub"}})
    before = db.get_tenant(tenant_id)
    assert before is not None and str(before.get("plan")) == "free"

    handler = StripeWebhookHandler(_FakeStripe(True), _FakeLedger(), tenant_store=db)
    result = handler.handle(
        _payload(tenant_id, "pro", subscription_id="sub_live"), "sig"
    )

    assert result["action"] == "plan_written"
    after = db.get_tenant(tenant_id)
    assert str(after.get("plan")) == "pro"
    assert after.get("subscription_status") == "active"
    assert after.get("stripe_subscription_id") == "sub_live"

    # replay на живой БД: состояние уже равно событию — записи не будет
    replay = handler.handle(
        _payload(tenant_id, "pro", subscription_id="sub_live"), "sig"
    )
    assert replay["action"] == "plan_unchanged"
