#!/usr/bin/env python3
"""Tests for _increment_usage — GPU seconds + token billing (new contract).

NB: _increment_usage now returns (total_cost, debited) and debits through
billing_ledger.debit_if_funds (PG-only, advisory lock + INSERT). These are unit
tests, so debit and metering are mocked; the PG debit path is covered by the
integration tests (concurrent "впритык → ровно один", idempotency).
"""

import pytest
import sys
sys.path.insert(0, ".")

from main import _increment_usage, metering_engine, billing_ledger
from billing.pg_ledger import PGUnavailableError


@pytest.fixture
def mock_billing(monkeypatch):
    debit_calls = []
    record_calls = []

    def fake_debit(tenant_id, amount, currency="USD", idempotency_key=None, **meta):
        debit_calls.append({
            "tenant_id": tenant_id, "amount": amount,
            "idempotency_key": idempotency_key, "meta": meta,
        })
        return "led-test"

    def fake_record(*args, **kwargs):
        record_calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(billing_ledger, "debit_if_funds", fake_debit)
    monkeypatch.setattr(metering_engine, "record", fake_record)
    return {"debit_calls": debit_calls, "record_calls": record_calls}


def test_increment_usage_gpu_seconds(mock_billing):
    total_cost, debited = _increment_usage(
        tenant_id="tenant_test_1", gpu_sec=100.0, plan_name="PRO", job_id="job-123",
    )
    assert total_cost == pytest.approx(0.001)  # 100 * 0.00001
    assert debited is True

    d = mock_billing["debit_calls"]
    assert len(d) == 1
    assert d[0]["tenant_id"] == "tenant_test_1"
    assert d[0]["amount"] == pytest.approx(0.001)
    assert d[0]["idempotency_key"] == "job:job-123"
    assert d[0]["meta"]["job_id"] == "job-123"
    assert d[0]["meta"]["gpu_sec"] == 100.0

    r = mock_billing["record_calls"]
    assert len(r) == 1
    kw = r[0]["kwargs"]
    assert kw["event_type"] == "cpu_execution"  # backend=None → local
    assert kw["billed"] is True
    assert kw["cost_usd"] == pytest.approx(0.001)
    assert kw["gpu_seconds"] == 100.0


def test_increment_usage_tokens(mock_billing):
    total_cost, debited = _increment_usage(
        tenant_id="tenant_test_2",
        input_tokens=1_000_000, output_tokens=500_000, job_id="job-456",
    )
    expected = 1_000_000 * 0.000001 + 500_000 * 0.000002  # 2.0
    assert total_cost == pytest.approx(2.0)
    assert debited is True

    d = mock_billing["debit_calls"]
    assert len(d) == 1
    assert d[0]["amount"] == pytest.approx(2.0)

    r = mock_billing["record_calls"]
    assert len(r) == 1
    kw = r[0]["kwargs"]
    assert kw["event_type"] == "token_usage"
    assert kw["billed"] is True
    assert kw["cost_usd"] == pytest.approx(2.0)
    assert kw["value"] == 1_500_000.0  # input+output as value (R15)


def test_increment_usage_both(mock_billing):
    total_cost, debited = _increment_usage(
        tenant_id="tenant_test_3",
        gpu_sec=50.0, input_tokens=100_000, output_tokens=50_000, job_id="job-789",
    )
    gpu_cost = 50 * 0.00001
    token_cost = 100_000 * 0.000001 + 50_000 * 0.000002
    assert total_cost == pytest.approx(gpu_cost + token_cost)
    assert debited is True

    # один дебет на ВСЮ сумму job'а (не два)
    d = mock_billing["debit_calls"]
    assert len(d) == 1
    assert d[0]["amount"] == pytest.approx(gpu_cost + token_cost)

    # две записи usage: cpu_execution + token_usage (разные классы ресурса)
    r = mock_billing["record_calls"]
    assert len(r) == 2
    event_types = {c["kwargs"]["event_type"] for c in r}
    assert event_types == {"cpu_execution", "token_usage"}


def test_increment_usage_zero_does_nothing(mock_billing):
    total_cost, debited = _increment_usage(tenant_id="tenant_zero")
    assert total_cost == 0.0
    assert debited is False
    assert mock_billing["debit_calls"] == []
    assert mock_billing["record_calls"] == []


def test_increment_usage_no_funds(mock_billing, monkeypatch):
    # дебет не прошёл → debited=False, usage НЕ пишется (но дебет пытались ровно раз)
    def no_funds(*a, **k):
        mock_billing["debit_calls"].append({"attempt": True})
        return None

    monkeypatch.setattr(billing_ledger, "debit_if_funds", no_funds)
    total_cost, debited = _increment_usage(
        tenant_id="tenant_test_4", gpu_sec=100.0, job_id="job-no-funds",
    )
    assert total_cost == pytest.approx(0.001)
    assert debited is False
    assert len(mock_billing["debit_calls"]) == 1  # ровно одна попытка дебита
    assert mock_billing["record_calls"] == []     # usage не пишется


def test_increment_usage_pg_error_raises(mock_billing, monkeypatch):
    # fail-closed: PG-ошибка дебита пробрасывается (worker ловит → billing:pg_error)
    def boom(*a, **k):
        raise PGUnavailableError("pool down")

    monkeypatch.setattr(billing_ledger, "debit_if_funds", boom)
    with pytest.raises(PGUnavailableError):
        _increment_usage("t", gpu_sec=1.0, job_id="j")


def test_increment_usage_vastai_gpu_execution(mock_billing):
    # Q1: backend="vastai" → event_type="gpu_execution" (не cpu_execution)
    total_cost, debited = _increment_usage(
        tenant_id="tenant_test_5", gpu_sec=10.0, job_id="job-vastai", backend="vastai",
    )
    assert total_cost == pytest.approx(0.0001)
    assert debited is True
    assert len(mock_billing["record_calls"]) == 1
    assert mock_billing["record_calls"][0]["kwargs"]["event_type"] == "gpu_execution"
