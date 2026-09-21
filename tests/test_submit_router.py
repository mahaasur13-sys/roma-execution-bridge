"""Isolated tests for routers/submit.py (no debit at submit — debit is in finalize)."""
import pytest
from fastapi.testclient import TestClient
import main

@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)

def test_submit_202_ok(client, monkeypatch):
    # мок verify_api_key, gate allowed, db insert — не трогать в этом скелете
    pytest.skip("заполнить моками: gate allowed → 202 queued · issue: R5b-ROUTING · expiry: 2026-10-31")

def test_submit_idempotency_same_key_no_duplicate_job(client, monkeypatch):
    pytest.skip("два submit с одним Idempotency-Key → один job_id · issue: R5b-ROUTING · expiry: 2026-10-31")

def test_submit_gate_deny_fail_closed(client, monkeypatch):
    pytest.skip("gate deny → 402, job не создан · issue: R5b-ROUTING · expiry: 2026-10-31")

def test_submit_tenant_scoped(client, monkeypatch):
    pytest.skip("чужой ключ не видит чужой job (404) · issue: R5b-ROUTING · expiry: 2026-10-31")

def test_submit_mask_key():
    pytest.skip("в логах только first4***last4 · issue: R5b-ROUTING · expiry: 2026-10-31")
