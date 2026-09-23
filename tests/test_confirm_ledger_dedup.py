"""G-CONFIRM-LEDGER-DOUBLE-WRITE (P3.9 hotfix): идемпотентность аудит-метки.

Дознание №45 (исход А) доказало: подтверждённая задача писала `job.user_confirmed`
дважды — `submit` → `route_job` → `execute_job` → `route_job`, а `write_event`
штамповал новый uuid4 на каждом проходе. Веер писателей был чист: дублировалась
только аудит-метка подтверждения.

Форма лечения предподписана владельцем: идемпотентность записи по ключу
(tenant_id, event_type, entity_id) — второй проход не пишет, если событие уже
стоит; новых таблиц/DDL/миграций нет (G-AUDIT-DDL-DRIFT — отдельная эпоха).
Инвариант хотфикса: `len(events) == 1` на одно подтверждение — включая полный
путь submit → execute.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import audit.event_store as audit_store
import db_adapter as db
import plan_source

PRO = "pro"
TENANT = "dedup-tenant"

_FORCED_VERDICT = {
    "decision": plan_source.REQUIRES_CONFIRMATION,
    "decision_category": "COST_ABOVE_TIER_LIMIT",
    "decision_reason": "принудительный вердикт (hotfix P3.9)",
    "estimated_cost": 41.0,
}


def _patch_tenant(monkeypatch, plan: str = PRO) -> None:
    monkeypatch.setattr(
        db, "get_tenant", lambda tenant_id: {"tenant_id": tenant_id, "plan": plan}
    )
    monkeypatch.setattr(
        db,
        "get_tenant_usage_db",
        lambda tenant_id: {"total_jobs": 0, "total_gpu_seconds": 0},
    )


def _scheduler():
    from scheduler.roma_scheduler import ROMAGPUScheduler

    sched = ROMAGPUScheduler.__new__(ROMAGPUScheduler)
    sched.policy_engine = None
    sched.local_mode = "local"
    sched.gate_unavailable_reason = None
    sched.gpu_connector = SimpleNamespace(is_available=lambda: False)
    sched.cost_gate = SimpleNamespace(
        evaluate=lambda tenant_id, payload: SimpleNamespace(
            result="allowed", reason="gate allowed"
        )
    )
    sched.predictor = SimpleNamespace(predict=lambda **kwargs: dict(_FORCED_VERDICT))
    calls: list = []

    def spy(job):
        calls.append(job.get("job_id"))
        return {
            "status": "success",
            "execution_target": "local",
            "stdout": "SPY_LOCAL_RAN",
        }

    sched._execute_local = spy
    sched._local_calls = calls
    return sched


def _job(job_id: str, **extra):
    job = {
        "job_id": job_id,
        "task_type": "train yolov8",
        "plugin_type": "ml_training",
        "gpu_required": False,
        "command": "python3 -c 'print(1)'",
        "tenant_id": TENANT,
    }
    job.update(extra)
    return job


def _ledger(monkeypatch) -> list:
    """Существующий леджерный путь + ключ идемпотентности (без таблиц/DDL).

    `write_event` — шпион, `event_exists` — состояние шпиона: как в реальном
    append-only леджере, записанный факт становится виден последующим проходам.
    """
    events: list = []
    keys: set = set()

    def spy_event_exists(tenant_id, event_type, entity_id):
        return (tenant_id, event_type, entity_id) in keys

    def spy_write_event(tenant_id, event_type, entity_type, entity_id, data):
        keys.add((tenant_id, event_type, entity_id))
        events.append(
            {
                "tenant_id": tenant_id,
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "data": data,
            }
        )
        return {"id": f"audit-{len(events)}"}

    monkeypatch.setattr(audit_store, "event_exists", spy_event_exists)
    monkeypatch.setattr(audit_store, "write_event", spy_write_event)
    return events


def _executor(sched):
    from scheduler.roma_scheduler import ROMAJobExecutor

    executor = ROMAJobExecutor.__new__(ROMAJobExecutor)
    executor.scheduler = sched
    executor.results = {}
    executor._job_ownership = {}
    return executor


def _confirmed_events(events: list) -> list:
    return [e for e in events if e["event_type"] == "job.user_confirmed"]


# ── инвариант: одно подтверждение — ровно одно событие ──────────────────────


def test_end_to_end_confirmed_job_writes_exactly_one_event(monkeypatch):
    """RED-якорь (№45): полный путь submit → execute давал ДВА события."""
    _patch_tenant(monkeypatch)
    events = _ledger(monkeypatch)
    sched = _scheduler()
    executor = _executor(sched)

    result = asyncio.run(executor.submit(_job("j-dedup-e2e", confirmed=True)))

    assert result["status"] == "success", result
    assert sched._local_calls == ["j-dedup-e2e"], sched._local_calls
    assert len(_confirmed_events(events)) == 1, events


def test_repeated_submit_of_same_job_keeps_single_event(monkeypatch):
    """Повторный submit той же задачи — копии факта нет (ключ идемпотентности)."""
    _patch_tenant(monkeypatch)
    events = _ledger(monkeypatch)

    for _ in range(2):
        sched = _scheduler()
        asyncio.run(_executor(sched).submit(_job("j-dedup-repeat", confirmed=True)))

    assert len(_confirmed_events(events)) == 1, events


def test_two_direct_route_passes_write_once(monkeypatch):
    """Двойной проход route_job на той же задаче (submit → execute_job) — 1 запись."""
    _patch_tenant(monkeypatch)
    events = _ledger(monkeypatch)
    sched = _scheduler()
    job = _job("j-dedup-two-passes", confirmed=True)

    first = sched.route_job(job)
    second = sched.route_job(job)

    assert first["status"] == "queued", first
    assert second["status"] == "queued", second
    assert len(_confirmed_events(events)) == 1, events


def test_different_confirmed_jobs_are_not_glued(monkeypatch):
    """Идемпотентность не склеивает чужие задачи: два job_id → два события."""
    _patch_tenant(monkeypatch)
    events = _ledger(monkeypatch)
    sched = _scheduler()

    sched.route_job(_job("j-dedup-a", confirmed=True))
    sched.route_job(_job("j-dedup-b", confirmed=True))

    confirmed = _confirmed_events(events)
    assert len(confirmed) == 2, events
    assert {e["entity_id"] for e in confirmed} == {"j-dedup-a", "j-dedup-b"}, confirmed


# ── контроли: не-подтверждённые ветки по-прежнему ничего не пишут ───────────


def test_controls_write_no_confirmation_event(monkeypatch):
    """Без флага — отказ; APPROVED — живой путь: событий подтверждения нет."""
    _patch_tenant(monkeypatch)
    events = _ledger(monkeypatch)

    rejected = _scheduler().route_job(_job("j-dedup-noflag"))
    assert rejected["status"] == "rejected", rejected
    assert rejected["reason"] == plan_source.CONFIRMATION_REQUIRED, rejected

    live = _scheduler()
    live.predictor = SimpleNamespace(
        predict=lambda **kwargs: {
            "decision": "APPROVED",
            "decision_category": None,
            "decision_reason": "в пределах лимита",
            "estimated_cost": 1.0,
        }
    )
    approved = live.route_job(_job("j-dedup-approved"))
    assert approved["status"] == "queued", approved
    assert approved["user_confirmed"] is False, approved

    assert events == [], events


# ── структурная наблюдаемость пропуска (без новых таблиц) ───────────────────


def test_write_event_once_reports_skip_structurally(monkeypatch):
    """Пропуск дубля наблюдаем возвратом `skipped: True` и не пишет в леджер."""
    written: list = []
    monkeypatch.setattr(audit_store, "event_exists", lambda t, e, i: True)
    monkeypatch.setattr(
        audit_store,
        "write_event",
        lambda *a, **k: written.append(a) or {"id": "must-not-happen"},
    )

    result = audit_store.write_event_once(
        TENANT, "job.user_confirmed", "job", "j-dedup-skip", {"user_confirmed": True}
    )

    assert result == {"id": None, "skipped": True}, result
    assert written == [], written
