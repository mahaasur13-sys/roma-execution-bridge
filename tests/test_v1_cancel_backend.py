"""POST /v1/jobs/{id}/cancel должен гасить backend-инстанс, как /cancel/{id}.

Инварианты:
  * смена статуса состоялась (n>0) → backend_cancel_job вызван ровно один раз;
  * повторная отмена и отмена терминальной задачи → вызова нет;
  * сбой backend_cancel_job не отменяет отмену: статус cancelled сохранён,
    сбой виден в логе и аудите, ответ клиенту — контракт /cancel/{id}.
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

import db_adapter as db
import main
import router_jobs
from deps import register_key


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _disable_background_worker(monkeypatch):
    async def _noop():
        return None

    monkeypatch.setattr(main, "init_worker", lambda: None)
    monkeypatch.setattr(main, "poll_and_execute", _noop)


@pytest.fixture()
def tenant(monkeypatch):
    tid = _uniq("test-cancel")
    key = _uniq("key-cancel")
    db.seed_tenants({key: {"tenant_id": tid, "name": "C"}})

    monkeypatch.setattr(main, "is_email_verified", lambda api_key: True)
    original = dict(main.API_KEYS)
    register_key(key, {"tenant_id": tid})

    yield {"tenant_id": tid, "key": key}

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


@pytest.fixture()
def backend_calls(monkeypatch):
    calls = []

    async def _fake(job_id, tenant_id=""):
        calls.append({"job_id": job_id, "tenant_id": tenant_id})
        return {"status": "destroyed", "job_id": job_id}

    monkeypatch.setattr(router_jobs, "backend_cancel_job", _fake)
    return calls


@pytest.fixture()
def audit_events(monkeypatch):
    captured = []
    real = router_jobs.write_audit_event

    def _spy(tenant_id, event_type, entity_type, entity_id, data=None):
        captured.append(
            {
                "tenant_id": tenant_id,
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "data": data or {},
            }
        )
        return real(tenant_id, event_type, entity_type, entity_id, data)

    monkeypatch.setattr(router_jobs, "write_audit_event", _spy)
    return captured


def test_cancel_running_destroys_backend_once(tenant, backend_calls):
    jid = _seed_job(tenant["tenant_id"])
    client = TestClient(main.app, raise_server_exceptions=False)

    resp = client.post(f"/v1/jobs/{jid}/cancel", headers={"X-API-Key": tenant["key"]})

    assert resp.status_code == 200
    assert resp.json() == {"job_id": jid, "status": "cancelled"}
    assert db.get_execution_job(jid)["status"] == "cancelled"
    assert backend_calls == [{"job_id": jid, "tenant_id": tenant["tenant_id"]}]


def test_repeat_cancel_and_terminal_job_do_not_touch_backend(tenant, backend_calls):
    jid = _seed_job(tenant["tenant_id"])
    done = _seed_job(tenant["tenant_id"], status="completed")
    client = TestClient(main.app, raise_server_exceptions=False)

    first = client.post(f"/v1/jobs/{jid}/cancel", headers={"X-API-Key": tenant["key"]})
    second = client.post(f"/v1/jobs/{jid}/cancel", headers={"X-API-Key": tenant["key"]})
    terminal = client.post(f"/v1/jobs/{done}/cancel", headers={"X-API-Key": tenant["key"]})

    assert first.status_code == 200
    # Отказ отдаёт policy_engine (403) либо validate_transition (409) — важен не код,
    # а то, что backend повторно не гасится.
    assert second.status_code in (403, 409)
    assert terminal.status_code in (403, 409)
    assert len(backend_calls) == 1
    assert backend_calls[0]["job_id"] == jid


def test_backend_failure_keeps_cancel_and_is_recorded(tenant, monkeypatch, audit_events):
    jid = _seed_job(tenant["tenant_id"])

    async def _boom(job_id, tenant_id=""):
        raise RuntimeError("vast api unavailable")

    class _LoggerSpy:
        def __init__(self):
            self.warnings = []

        def warning(self, msg, *args):
            self.warnings.append(msg % args if args else msg)

        def __getattr__(self, name):
            return lambda *a, **k: None

    spy = _LoggerSpy()
    monkeypatch.setattr(router_jobs, "backend_cancel_job", _boom)
    monkeypatch.setattr(router_jobs, "logger", spy)
    client = TestClient(main.app, raise_server_exceptions=False)

    resp = client.post(f"/v1/jobs/{jid}/cancel", headers={"X-API-Key": tenant["key"]})
    logged = "\n".join(spy.warnings)

    assert resp.status_code == 200
    assert resp.json() == {"job_id": jid, "status": "cancelled"}
    assert db.get_execution_job(jid)["status"] == "cancelled"
    assert "cancel.backend_cleanup_failed" in logged
    failures = [e for e in audit_events if e["event_type"] == "job.cancel.backend_cleanup_failed"]
    assert len(failures) == 1
    assert failures[0]["entity_id"] == jid
    assert "vast api unavailable" in failures[0]["data"]["error"]
