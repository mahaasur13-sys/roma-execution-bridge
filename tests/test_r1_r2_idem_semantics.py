"""R1+R2 — семантика debit_if_funds: реплей vs неопределённый COMMIT vs mismatch.

R1: неопределённый COMMIT (PGUnavailableError), при котором строка реально
    закоммитилась ЭТИМ вызовом, возвращает is_new=True (usage/метрики пишутся).
R2: mismatch суммы/типа/валюты/job_id по ключу идемпотентности → отдельная
    ошибка LedgerIdemMismatchError (не «нет средств»/None), finalize → "mismatch".
"""

import hashlib
import json

import pytest

from billing.pg_ledger import PGBillingLedger, LedgerIdemMismatchError
from billing.pg_connection import PGUnavailableError


@pytest.fixture
def ledger():
    return PGBillingLedger()


def _ledger_id(tenant, key):
    return "led-" + hashlib.sha256(f"{tenant}:{key}".encode()).hexdigest()[:24]


def test_r1_indeterminate_commit_returns_is_new_true(ledger, monkeypatch):
    tenant, key, amount, job_id = "t-r1", "job:j1", 1.0, "j1"
    lid = _ledger_id(tenant, key)

    def boom(*a, **k):
        raise PGUnavailableError("pg down (test)")

    def fake_lookup(op, q, params, fetch=False):
        return [("DEBIT", amount, "USD", json.dumps({"job_id": job_id}))]

    monkeypatch.setattr(ledger, "_txn", boom)
    monkeypatch.setattr(ledger, "_pg_execute", fake_lookup)

    rid, is_new = ledger.debit_if_funds(tenant, amount, idempotency_key=key, job_id=job_id)
    assert rid == lid
    assert is_new is True, "R1: строка закоммитилась этим вызовом — это новый дебет"


def test_r1_indeterminate_retry_idempotent_debit(ledger, monkeypatch):
    tenant, key, amount, job_id = "t-r1-retry", "job:j1r", 1.0, "j1r"
    lid = _ledger_id(tenant, key)

    def boom(*a, **k):
        raise PGUnavailableError("pg down (test)")

    def fake_lookup(op, q, params, fetch=False):
        return [("DEBIT", amount, "USD", json.dumps({"job_id": job_id}))]

    monkeypatch.setattr(ledger, "_txn", boom)
    monkeypatch.setattr(ledger, "_pg_execute", fake_lookup)

    rid1, _ = ledger.debit_if_funds(tenant, amount, idempotency_key=key, job_id=job_id)
    rid2, _ = ledger.debit_if_funds(tenant, amount, idempotency_key=key, job_id=job_id)
    assert rid1 == rid2 == lid, "повторный неопределённый COMMIT не должен дать двойной DEBIT"
    # Признак «новый usage» теперь берётся из rowcount usage_events-INSERT
    # (см. test_ot74_replay_skips_metrics / _increment_usage), а не из is_new.


def test_r2_mismatch_raises_distinct_error(ledger, monkeypatch):
    tenant, key, amount, job_id = "t-r2", "job:j2", 1.0, "j2"

    monkeypatch.setattr(ledger, "_txn", lambda *a, **k: [])  # no rows → replay path
    monkeypatch.setattr(
        ledger,
        "_pg_execute",
        lambda op, q, params, fetch=False: [("DEBIT", 999.0, "USD", json.dumps({"job_id": "other"}))],
    )

    with pytest.raises(LedgerIdemMismatchError):
        ledger.debit_if_funds(tenant, amount, idempotency_key=key, job_id=job_id)


def test_r2_finalize_returns_mismatch_not_no_funds(monkeypatch):
    import db_adapter as db
    from billing import finalize as fin

    class FakeLedger:
        def debit_if_funds(self, *a, **k):
            raise LedgerIdemMismatchError("mismatch (test)")

    monkeypatch.setattr(fin, "billing_ledger", FakeLedger())
    monkeypatch.setattr(db, "get_execution_job", lambda jid: {"id": jid, "tenant_id": "t", "status": "running"})

    assert fin.finalize_job_billing("t", "j2", 1.0, "free") == "mismatch"
