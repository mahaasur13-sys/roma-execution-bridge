"""P0 security regression tests for the hotfix branch.

Covers:
  - worker /execute auth + command/image/mount policy (gpu_worker/server.py)
  - /billing/top-up admin gating (main.py)
  - job mutation authz: /complete/{id}, /v1/jobs/{id}/complete, /worker-ack
  - _tenant_from_key resolves the real tenant (router_jobs.py)

Run with the project venv (requires pydantic_settings for the crypto import chain):
    /home/felix/dsh-workspace/.venv-roma/bin/python -m pytest tests/test_p0_security.py -q
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient
from fastapi import HTTPException

import db_adapter as db
import main
import gpu_worker.server as gws


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _disable_background_worker(monkeypatch):
    """Prevent startup from launching the infinite poll_and_execute loop."""
    async def _noop():
        return None

    monkeypatch.setattr(main, "init_worker", lambda: None)
    monkeypatch.setattr(main, "poll_and_execute", _noop)


@pytest.fixture()
def tenants(monkeypatch):
    """Seed two distinct tenants A/B with their own API keys."""
    a_id = _uniq("t-a")
    b_id = _uniq("t-b")
    key_a = _uniq("key-a")
    key_b = _uniq("key-b")

    db.seed_tenants({
        key_a: {"tenant_id": a_id, "name": "A"},
        key_b: {"tenant_id": b_id, "name": "B"},
    })

    # verify_api_key enforces email verification for non-admin keys — bypass it.
    monkeypatch.setattr(main, "is_email_verified", lambda api_key: True)

    # _admin_only reads main.API_KEYS (in-memory registry).
    original = dict(main.API_KEYS)
    main.API_KEYS.update({
        key_a: {"tenant_id": a_id},
        key_b: {"tenant_id": b_id},
    })
    main.API_KEYS["hotfix-admin-demo"] = {"tenant_id": "tenant-demo"}

    yield {"a_id": a_id, "b_id": b_id, "key_a": key_a, "key_b": key_b}

    main.API_KEYS.clear()
    main.API_KEYS.update(original)


def _seed_job(tenant_id, status="running"):
    jid = _uniq("job")
    db.insert_execution_job(
        job_id=jid,
        decision_id=_uniq("dec"),
        tenant_id=tenant_id,
        status=status,
        payload={},
    )
    return jid


# ─────────────────────────────────────────────────────────────────────
# /billing/top-up — admin gating
# ─────────────────────────────────────────────────────────────────────

def test_top_up_non_admin_forbidden(tenants):
    client = TestClient(main.app, raise_server_exceptions=False)
    before = main.billing_ledger.get_balance(tenants["b_id"])
    resp = client.post(
        "/billing/top-up",
        json={"tenant_id": tenants["b_id"], "amount": 999.0},
        headers={"X-API-Key": tenants["key_a"]},
    )
    assert resp.status_code == 403
    assert main.billing_ledger.get_balance(tenants["b_id"]) == before


def test_top_up_cannot_credit_other_tenant(tenants):
    client = TestClient(main.app, raise_server_exceptions=False)
    before = main.billing_ledger.get_balance(tenants["b_id"])
    # Non-admin tenant A attempts to credit tenant B — must be rejected.
    resp = client.post(
        "/billing/top-up",
        json={"tenant_id": tenants["b_id"], "amount": 500.0},
        headers={"X-API-Key": tenants["key_a"]},
    )
    assert resp.status_code == 403
    assert main.billing_ledger.get_balance(tenants["b_id"]) == before


# ─────────────────────────────────────────────────────────────────────
# Job mutation authz (main.py + router_jobs.py)
# ─────────────────────────────────────────────────────────────────────

def test_complete_requires_auth(tenants):
    jid = _seed_job(tenants["a_id"])
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = client.post(f"/complete/{jid}")
    assert resp.status_code == 401
    assert db.get_execution_job(jid)["status"] == "running"


def test_complete_cross_tenant_404(tenants):
    jid = _seed_job(tenants["a_id"])
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = client.post(f"/complete/{jid}", headers={"X-API-Key": tenants["key_b"]})
    assert resp.status_code == 404
    assert db.get_execution_job(jid)["status"] == "running"


def test_v1_complete_requires_auth(tenants):
    jid = _seed_job(tenants["a_id"])
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = client.post(f"/v1/jobs/{jid}/complete")
    assert resp.status_code == 401
    assert db.get_execution_job(jid)["status"] == "running"


def test_v1_complete_cross_tenant_404(tenants):
    jid = _seed_job(tenants["a_id"])
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = client.post(f"/v1/jobs/{jid}/complete", headers={"X-API-Key": tenants["key_b"]})
    assert resp.status_code == 404
    assert db.get_execution_job(jid)["status"] == "running"


def test_worker_ack_cross_tenant_404(tenants):
    jid = _seed_job(tenants["a_id"])
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = client.post(
        f"/v1/jobs/{jid}/worker-ack",
        json={"action": "complete"},
        headers={"X-API-Key": tenants["key_b"]},
    )
    assert resp.status_code == 404
    assert db.get_execution_job(jid)["status"] == "running"


# ─────────────────────────────────────────────────────────────────────
# _tenant_from_key
# ─────────────────────────────────────────────────────────────────────

def test_tenant_from_key_returns_real_tenant(tenants):
    from router_jobs import _tenant_from_key
    assert _tenant_from_key(tenants["key_a"]) == tenants["a_id"]
    assert _tenant_from_key(tenants["key_b"]) == tenants["b_id"]


def test_tenant_from_key_invalid_key_401(tenants):
    from router_jobs import _tenant_from_key
    with pytest.raises(HTTPException) as exc_info:
        _tenant_from_key("nonexistent-key-xyz")
    assert exc_info.value.status_code == 401


# ─────────────────────────────────────────────────────────────────────
# gpu_worker /execute — worker token + execution policy
# ─────────────────────────────────────────────────────────────────────

def test_execute_requires_worker_token(monkeypatch):
    monkeypatch.setattr(gws, "WORKER_TOKEN", "sekret-token")
    monkeypatch.setattr(gws.state, "gpu_available", True)
    called = []
    monkeypatch.setattr(gws.subprocess, "run", lambda *a, **k: called.append(a))

    client = TestClient(gws.app, raise_server_exceptions=False)
    resp = client.post(
        "/execute",
        json={"job_id": "j1", "command": "python -c 'print(1)'"},
    )
    assert resp.status_code == 401
    assert called == []  # command must NOT run


def test_execute_wrong_worker_token(monkeypatch):
    monkeypatch.setattr(gws, "WORKER_TOKEN", "sekret-token")
    monkeypatch.setattr(gws.state, "gpu_available", True)
    called = []
    monkeypatch.setattr(gws.subprocess, "run", lambda *a, **k: called.append(a))

    client = TestClient(gws.app, raise_server_exceptions=False)
    resp = client.post(
        "/execute",
        json={"job_id": "j1", "command": "python -c 'print(1)'"},
        headers={"X-Roma-Worker-Token": "wrong"},
    )
    assert resp.status_code == 401
    assert called == []


def test_execute_rejects_command_outside_allowlist(monkeypatch):
    monkeypatch.setattr(gws, "WORKER_TOKEN", "sekret-token")
    monkeypatch.setattr(gws.state, "gpu_available", True)
    called = []
    monkeypatch.setattr(gws.subprocess, "run", lambda *a, **k: called.append(a))

    client = TestClient(gws.app, raise_server_exceptions=False)
    resp = client.post(
        "/execute",
        json={"job_id": "j1", "command": "sh -c 'id'"},
        headers={"X-Roma-Worker-Token": "sekret-token"},
    )
    assert resp.status_code == 403
    assert called == []


def test_execute_rejects_mount_paths(monkeypatch):
    monkeypatch.setattr(gws, "WORKER_TOKEN", "sekret-token")
    monkeypatch.setattr(gws.state, "gpu_available", True)
    called = []
    monkeypatch.setattr(gws.subprocess, "run", lambda *a, **k: called.append(a))

    client = TestClient(gws.app, raise_server_exceptions=False)
    resp = client.post(
        "/execute",
        json={
            "job_id": "j1",
            "command": "python -c 'print(1)'",
            "mount_paths": {"/host/etc": "/etc"},
        },
        headers={"X-Roma-Worker-Token": "sekret-token"},
    )
    assert resp.status_code == 403
    assert called == []


def test_execute_rejects_image_outside_allowlist(monkeypatch):
    monkeypatch.setattr(gws, "WORKER_TOKEN", "sekret-token")
    monkeypatch.setattr(gws.state, "gpu_available", True)
    called = []
    monkeypatch.setattr(gws.subprocess, "run", lambda *a, **k: called.append(a))

    client = TestClient(gws.app, raise_server_exceptions=False)
    resp = client.post(
        "/execute",
        json={"job_id": "j1", "command": "python -c 'print(1)'", "image": "alpine:latest"},
        headers={"X-Roma-Worker-Token": "sekret-token"},
    )
    assert resp.status_code == 403
    assert called == []


def test_validate_execute_job_allowlist():
    # allowed binary → argv list (no shell)
    argv = gws._validate_execute_job(
        gws.JobRequest(job_id="j1", command="python train.py --epochs 1")
    )
    assert argv == ["python", "train.py", "--epochs", "1"]

    # non-allowlisted binary → rejected
    with pytest.raises(HTTPException) as e1:
        gws._validate_execute_job(gws.JobRequest(job_id="j1", command="sh -c 'id'"))
    assert e1.value.status_code == 403

    # shell metacharacter binary is not in the allowlist → rejected
    with pytest.raises(HTTPException) as e2:
        gws._validate_execute_job(gws.JobRequest(job_id="j1", command="python; rm -rf /"))
    assert e2.value.status_code == 403

    # any mount_paths → rejected
    with pytest.raises(HTTPException) as e3:
        gws._validate_execute_job(
            gws.JobRequest(job_id="j1", command="python -c 1", mount_paths={"/host": "/c"})
        )
    assert e3.value.status_code == 403

    # arbitrary image → rejected
    with pytest.raises(HTTPException) as e4:
        gws._validate_execute_job(
            gws.JobRequest(job_id="j1", command="python -c 1", image="alpine:latest")
        )
    assert e4.value.status_code == 403


def test_worker_token_fail_closed_when_unset(monkeypatch):
    monkeypatch.setattr(gws, "WORKER_TOKEN", "")
    assert gws._worker_token_valid("anything") is False
    assert gws._worker_token_valid("") is False


def test_worker_token_valid(monkeypatch):
    monkeypatch.setattr(gws, "WORKER_TOKEN", "sekret-token")
    assert gws._worker_token_valid("sekret-token") is True
    assert gws._worker_token_valid("wrong") is False
    assert gws._worker_token_valid(None) is False
