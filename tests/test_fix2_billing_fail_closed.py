"""FIX2 (аудит 55b9ed44): fail-closed по деньгам — пять находок одним файлом.

  1. worker-ack ``action=fail`` по legacy-ключу арендатора закрывал running-job без
     дебета (бесплатная работа) → только server-to-server токен воркера.
  2. Нет подтверждённой длительности и нет/битый/будущий ``started_at`` → прежде
     gpu_sec=0 (стоимость 0 → finalize отдавал «no_funds» → 402 «Insufficient funds»);
     теперь явный 409 ``billing_duration_unknown``, дебета нет, статус не меняется.
  3. ``PGBillingLedger.append()``/``debit()`` при недоступном PG оставляли запись
     только в памяти → поднимают ``PGUnavailableError``, память не пополняется.
  4. ``get_tenant_balance()`` при недоступном PG молча считал баланс из памяти →
     поднимает ``PGUnavailableError``; ``/billing/balance|spend-cap|ledger`` → 503;
     gauge баланса после уже сохранённого дебета не превращает успех в 503.
  5. ``POST /billing/top-up`` отклоняет NaN/Inf/0/отрицательные (тест на уже
     существующую проверку) + ledger-уровень отклоняет не-finite/отрицательные суммы.

Без сети и без живого PG: job-хранилище и ledger подменены, тенанты — штатный seed.
"""

from __future__ import annotations

import hashlib
import math
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

import db_adapter as db
import main
from billing import finalize as fin
from billing.pg_ledger import PGBillingLedger, PGUnavailableError
from deps import register_key

WORKER_TOKEN = "fix2-test-worker-token"


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class FakeLedger:
    """debit_if_funds с детерминированным ключом; считает вызовы."""

    def __init__(self, balance: float = 100.0):
        self.balance = balance
        self.calls = 0
        self.debits: dict[str, float] = {}
        self.balance_pg_down = False

    def get_tenant_balance(self, tenant_id):
        if self.balance_pg_down:
            raise PGUnavailableError("pg down (test)")
        return self.balance

    def debit_if_funds(self, tenant_id, amount, currency="USD", idempotency_key=None, **meta):
        self.calls += 1
        lid = "led-" + hashlib.sha256(f"{tenant_id}:{idempotency_key}".encode()).hexdigest()[:24]
        if lid in self.debits:
            return lid, False
        if self.balance < amount:
            return None, False
        self.balance -= amount
        self.debits[lid] = amount
        return lid, True


@pytest.fixture(autouse=True)
def _disable_background_worker(monkeypatch):
    async def _noop():
        return None

    monkeypatch.setattr(main, "init_worker", lambda: None)
    monkeypatch.setattr(main, "poll_and_execute", _noop)


@pytest.fixture()
def world(monkeypatch):
    """Тенант (штатный seed) + подменённое job-хранилище + FakeLedger в finalize."""
    import router_jobs
    import routers.public_jobs as public_jobs

    tenant_id = _uniq("test-fix2")
    key = _uniq("key-fix2")
    db.seed_tenants({key: {"tenant_id": tenant_id, "name": "F2"}})
    monkeypatch.setattr(main, "is_email_verified", lambda api_key: True)
    original = dict(main.API_KEYS)
    register_key(key, {"tenant_id": tenant_id})

    jobs: dict[str, dict] = {}

    def _get(job_id):
        j = jobs.get(job_id)
        return dict(j) if j else None

    def _update(job_id, status=None, completed_at=None, error=None, tenant_id=None, if_status_not_in=None, **kw):
        j = jobs.get(job_id)
        if not j:
            return 0
        if if_status_not_in and j.get("status") in if_status_not_in:
            return 0
        if status is not None:
            j["status"] = status
        if completed_at is not None:
            j["completed_at"] = completed_at
        if error is not None:
            j["error"] = error
        return 1

    monkeypatch.setattr(db, "get_execution_job", _get)
    monkeypatch.setattr(db, "update_execution_job", _update)

    async def _no_confirmed(job_id, backend_name=None):
        return None

    async def _no_backend(**kw):
        return None

    monkeypatch.setattr(router_jobs, "get_confirmed_duration", _no_confirmed)
    monkeypatch.setattr(public_jobs, "get_confirmed_duration", _no_confirmed)
    monkeypatch.setattr(public_jobs, "backend_cancel_job", _no_backend)
    monkeypatch.setattr(router_jobs, "write_audit_event", lambda *a, **k: None)
    monkeypatch.setenv("ROMA_GPU_WORKER_TOKEN", WORKER_TOKEN)

    ledger = FakeLedger()
    monkeypatch.setattr(fin, "billing_ledger", ledger)
    monkeypatch.setattr(fin.metering_engine, "record", lambda **k: (None, True))

    def add_job(started_at="past", status="running"):
        jid = _uniq("job-fix2")
        job = {"job_id": jid, "tenant_id": tenant_id, "status": status, "backend": None, "payload": {}}
        if started_at == "past":
            job["started_at"] = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        elif started_at is not None:
            job["started_at"] = started_at
        jobs[jid] = job
        return jid

    yield {
        "tenant_id": tenant_id,
        "key": key,
        "jobs": jobs,
        "ledger": ledger,
        "add_job": add_job,
        "client": TestClient(main.app, raise_server_exceptions=False),
    }

    main.API_KEYS.clear()
    main.API_KEYS.update(original)


# ── 1. worker-ack fail по legacy-ключу арендатора ───────────────────────────


def test_worker_ack_fail_with_tenant_key_is_forbidden(world):
    jid = world["add_job"]()
    resp = world["client"].post(
        f"/v1/jobs/{jid}/worker-ack",
        json={"action": "fail", "error": "boom"},
        headers={"X-API-Key": world["key"]},
    )
    assert resp.status_code == 403, resp.text
    assert world["jobs"][jid]["status"] == "running"


def test_worker_ack_fail_cross_tenant_still_404(world):
    """Порядок проверок не меняется: чужой job → 404 раньше 403."""
    jid = world["add_job"]()
    world["jobs"][jid]["tenant_id"] = "someone-else"
    resp = world["client"].post(
        f"/v1/jobs/{jid}/worker-ack",
        json={"action": "fail"},
        headers={"X-API-Key": world["key"]},
    )
    assert resp.status_code == 404


def test_worker_ack_fail_with_worker_token_still_allowed(world):
    jid = world["add_job"]()
    resp = world["client"].post(
        f"/v1/jobs/{jid}/worker-ack",
        json={"action": "fail", "error": "oom"},
        headers={"X-Roma-Worker-Token": WORKER_TOKEN},
    )
    assert resp.status_code == 200, resp.text
    assert world["jobs"][jid]["status"] == "failed"


def test_worker_ack_complete_with_tenant_key_is_billed(world):
    jid = world["add_job"]()
    resp = world["client"].post(
        f"/v1/jobs/{jid}/worker-ack",
        json={"action": "complete"},
        headers={"X-API-Key": world["key"]},
    )
    assert resp.status_code == 200, resp.text
    assert world["jobs"][jid]["status"] == "completed"
    assert world["ledger"].calls == 1 and len(world["ledger"].debits) == 1


# ── 2. нулевая длительность без started_at / confirmed ──────────────────────

_ENDPOINTS = ("public_complete", "v1_complete", "worker_ack_complete")
_BAD_STARTED = (None, "not-a-timestamp", "FUTURE")


def _call(world, endpoint, jid):
    c = world["client"]
    if endpoint == "public_complete":
        return c.post(f"/complete/{jid}", headers={"X-API-Key": world["key"]})
    if endpoint == "v1_complete":
        return c.post(f"/v1/jobs/{jid}/complete", headers={"X-API-Key": world["key"]})
    return c.post(
        f"/v1/jobs/{jid}/worker-ack",
        json={"action": "complete"},
        headers={"X-Roma-Worker-Token": WORKER_TOKEN},
    )


@pytest.mark.parametrize("started", _BAD_STARTED, ids=("missing", "garbage", "future"))
@pytest.mark.parametrize("endpoint", _ENDPOINTS)
def test_unknown_duration_refuses_completion(world, endpoint, started):
    if started == "FUTURE":
        started = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    jid = world["add_job"](started_at=started)
    resp = _call(world, endpoint, jid)
    assert resp.status_code == 409, resp.text
    assert "billing_duration_unknown" in resp.text
    assert world["jobs"][jid]["status"] == "running"
    assert world["ledger"].calls == 0


@pytest.mark.parametrize("endpoint", _ENDPOINTS)
def test_known_duration_completes_and_debits_once(world, endpoint):
    jid = world["add_job"]()
    resp = _call(world, endpoint, jid)
    assert resp.status_code == 200, resp.text
    assert world["jobs"][jid]["status"] == "completed"
    assert len(world["ledger"].debits) == 1
    (amount,) = world["ledger"].debits.values()
    assert amount > 0


def test_resolve_billable_seconds_contract():
    from billing.finalize import resolve_billable_seconds

    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    assert resolve_billable_seconds(42.0, None, now=now) == 42.0
    assert resolve_billable_seconds(None, "2026-11-04T11:59:00Z", now=now) == 60.0
    assert resolve_billable_seconds(None, "2026-11-04T11:59:00", now=now) == 60.0  # naive → UTC
    assert resolve_billable_seconds(None, None, now=now) is None
    assert resolve_billable_seconds(None, "", now=now) is None
    assert resolve_billable_seconds(None, "garbage", now=now) is None
    assert resolve_billable_seconds(None, "2026-11-04T13:00:00Z", now=now) is None  # будущее
    # Невалидный confirmed (NaN/Inf/отрицательный/мусор) не принимается как длительность.
    for bad in (float("nan"), float("inf"), -5.0, "x"):
        assert resolve_billable_seconds(bad, None, now=now) is None
        assert resolve_billable_seconds(bad, "2026-11-04T11:59:00Z", now=now) == 60.0


# ── 3. append()/debit() fail-closed ─────────────────────────────────────────


def _pg_down(*args, **kwargs):
    raise PGUnavailableError("PG not configured or unavailable")


def test_append_raises_on_pg_down_and_keeps_no_memory_entry(monkeypatch):
    ledger = PGBillingLedger()
    monkeypatch.setattr(ledger, "_pg_execute", _pg_down)
    with pytest.raises(PGUnavailableError):
        ledger.append("t-fix2", "DEBIT", 1.0)
    assert ledger._entries == []


def test_debit_raises_on_pg_down(monkeypatch):
    ledger = PGBillingLedger()
    monkeypatch.setattr(ledger, "_pg_execute", _pg_down)
    with pytest.raises(PGUnavailableError):
        ledger.debit("t-fix2", 1.0)
    assert ledger._entries == []


def test_append_mirrors_only_after_pg_write(monkeypatch):
    ledger = PGBillingLedger()
    seen: list[int] = []

    def _ok(operation, query, params=None, fetch=False):
        seen.append(len(ledger._entries))  # зеркало ещё пустое в момент записи в PG
        return None

    monkeypatch.setattr(ledger, "_pg_execute", _ok)
    lid = ledger.append("t-fix2", "CREDIT", 2.0)
    assert seen == [0]
    assert [e["ledger_id"] for e in ledger._entries] == [lid]


# ── 4. get_tenant_balance fail-closed + 503 в роутерах ──────────────────────


def test_get_tenant_balance_raises_on_pg_down(monkeypatch):
    ledger = PGBillingLedger()
    ledger._entries.append(
        {
            "ledger_id": "led-x",
            "timestamp": 0,
            "tenant_id": "t-fix2",
            "type": "CREDIT",
            "amount": 5.0,
            "currency": "USD",
            "metadata": {},
        }
    )
    monkeypatch.setattr(ledger, "_pg_execute", _pg_down)
    with pytest.raises(PGUnavailableError):
        ledger.get_tenant_balance("t-fix2")
    with pytest.raises(PGUnavailableError):
        ledger.get_balance("t-fix2")


@pytest.mark.parametrize("path", ("/billing/balance", "/billing/spend-cap?estimated_cost_usd=0.01", "/billing/ledger"))
def test_billing_balance_routes_return_503_on_pg_down(world, monkeypatch, path):
    import routers.billing as billing_router

    ledger = PGBillingLedger()
    monkeypatch.setattr(ledger, "_pg_execute", _pg_down)
    monkeypatch.setattr(billing_router, "billing_ledger", ledger)
    resp = world["client"].get(path, headers={"X-API-Key": world["key"]})
    assert resp.status_code == 503, resp.text


def test_finalize_ok_when_only_balance_gauge_unavailable(world):
    """Дебет уже сохранён → недоступный gauge баланса не превращает успех в 503."""
    jid = world["add_job"]()
    world["ledger"].balance_pg_down = True
    status = fin.finalize_job_billing(world["tenant_id"], jid, 60.0, "free")
    assert status == "ok"
    assert len(world["ledger"].debits) == 1


# ── 5. top-up: NaN/Inf/0/отрицательные + ledger-уровень ─────────────────────


class _CreditSpy:
    def __init__(self):
        self.calls = []

    def credit(self, tenant_id, amount, **kw):
        self.calls.append((tenant_id, amount))
        return "led-spy"


@pytest.fixture()
def admin_client(monkeypatch):
    import deps
    import routers.billing as billing_router

    spy = _CreditSpy()
    monkeypatch.setattr(billing_router, "billing_ledger", spy)
    main.limiter.reset()  # лимит 10/min на top-up — не мерить его этим тестом
    monkeypatch.setenv("ROMA_ADMIN_KEYS", "test-admin-key")
    monkeypatch.setattr(deps, "ADMIN_IP_ALLOWLIST", "*")
    return TestClient(main.app, raise_server_exceptions=False), spy


@pytest.mark.parametrize(
    "amount",
    ("nan", "NaN", "inf", "-inf", "Infinity", 0, 0.0, "-0.0", -1, -0.01, "abc", None),
    ids=(
        "nan",
        "NaN",
        "inf",
        "-inf",
        "Infinity",
        "zero-int",
        "zero-float",
        "neg-zero",
        "neg-1",
        "neg-cent",
        "garbage",
        "none",
    ),
)
def test_top_up_rejects_non_finite_zero_negative(admin_client, amount):
    client, spy = admin_client
    resp = client.post(
        "/billing/top-up",
        json={"tenant_id": _uniq("test-fix2-target"), "amount": amount},
        headers={"X-API-Key": "test-admin-key"},
    )
    assert resp.status_code == 400, resp.text
    assert spy.calls == []


def test_top_up_accepts_positive_finite(admin_client):
    client, spy = admin_client
    target = _uniq("test-fix2-target")
    resp = client.post(
        "/billing/top-up",
        json={"tenant_id": target, "amount": 3.5},
        headers={"X-API-Key": "test-admin-key"},
    )
    assert resp.status_code == 200, resp.text
    assert spy.calls == [(target, 3.5)]


@pytest.mark.parametrize("amount", (float("nan"), float("inf"), float("-inf"), -1.0))
@pytest.mark.parametrize("op", ("append", "credit"))
def test_ledger_rejects_non_finite_or_negative_amount_before_pg(monkeypatch, op, amount):
    ledger = PGBillingLedger()
    calls: list[str] = []

    def _record(operation, query, params=None, fetch=False):
        calls.append(operation)
        return [("led-x",)] if fetch else None

    monkeypatch.setattr(ledger, "_pg_execute", _record)
    with pytest.raises(ValueError, match=r"ledger_entries\.amount"):
        if op == "append":
            ledger.append("t-fix2", "DEBIT", amount)
        else:
            ledger.credit("t-fix2", amount)
    assert calls == [] and ledger._entries == []


def test_reject_invalid_money_amount_contract():
    from cost.money_policy import reject_invalid_money_amount

    for ok in (0, 0.0, 1, 2.5):
        reject_invalid_money_amount("ledger_entries", "amount", ok)
    reject_invalid_money_amount("ledger_entries", "amount", None)  # None — договор reject_nullable_money
    for bad in (math.nan, math.inf, -math.inf, -0.01, "abc"):
        with pytest.raises(ValueError, match=r"ledger_entries\.amount"):
            reject_invalid_money_amount("ledger_entries", "amount", bad)
