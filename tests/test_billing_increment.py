#!/usr/bin/env python3
"""Tests for _increment_usage — GPU seconds + token billing."""

import pytest
import sys
sys.path.insert(0, ".")

from main import _increment_usage, metering_engine, billing_ledger


@pytest.fixture(autouse=True)
def clean_state():
    """Clear singletons before each test."""
    metering_engine.events.clear()
    metering_engine.tenant_totals.clear()
    billing_ledger._entries.clear()
    yield


def test_increment_usage_gpu_seconds():
    cost = _increment_usage(
        tenant_id="tenant_test_1",
        gpu_sec=100.0,
        plan_name="PRO",
        job_id="job-123",
    )
    assert cost == pytest.approx(0.001)  # 100 * 0.00001
    assert len(metering_engine.events) == 1
    assert len(billing_ledger._entries) == 1
    entry = billing_ledger._entries[0]
    assert entry["type"] == "DEBIT"
    assert entry["amount"] == pytest.approx(0.001)
    assert entry.get("metadata", {}).get("gpu_sec") == 100.0


def test_increment_usage_tokens():
    cost = _increment_usage(
        tenant_id="tenant_test_2",
        input_tokens=1_000_000,
        output_tokens=500_000,
        job_id="job-456",
    )
    expected = 1_000_000 * 0.000001 + 500_000 * 0.000002  # 1.0 + 1.0 = 2.0
    assert cost == pytest.approx(2.0)
    assert len(billing_ledger._entries) == 1
    assert billing_ledger._entries[0]["amount"] == pytest.approx(2.0)


def test_increment_usage_both():
    cost = _increment_usage(
        tenant_id="tenant_test_3",
        gpu_sec=50.0,
        input_tokens=100_000,
        output_tokens=50_000,
        job_id="job-789",
    )
    gpu_cost = 50 * 0.00001
    token_cost = 100_000 * 0.000001 + 50_000 * 0.000002
    assert cost == pytest.approx(gpu_cost + token_cost)
    assert len(billing_ledger._entries) == 2


def test_increment_usage_zero_does_nothing():
    cost = _increment_usage(tenant_id="tenant_zero")
    assert cost == 0.0
    assert len(billing_ledger._entries) == 0
