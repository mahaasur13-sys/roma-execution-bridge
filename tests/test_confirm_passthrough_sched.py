"""G-CONFIRM-PASSTHROUGH-SCHED (P3.7): подтверждение крупной сметы — отказ-контракт.

Решение владельца (меморандум 2026-09-23, вариант B) — binding:

  * вердикт `REQUIRES_CONFIRMATION` на scheduler-пути означает НЕ «роутится как
    прежде», а отказ-контракт: без строгого boolean `confirmed: true` в задаче
    маршрут `rejected` с кодом `CONFIRMATION_REQUIRED` и подсказкой повтора
    (одна и та же задача + флаг);
  * с `confirmed: true` задача маршрутизируется как прежде, а подтверждение
    записывается фактом в существующем леджерном пути (`audit_events.data`,
    `user_confirmed: true`) — новых таблиц/файлов/секретов нет;
  * любая иная форма флага (строка, `1`, None, отсутствие) — отказ: fail-closed;
  * недоступный леджер подтверждения блокирует исполнение классом
    `GATE_UNAVAILABLE`: подтверждение обязано быть аудируемым фактом.

Дверь сегодня латентная (потолок сметы оценщика $0.011 против порога
подтверждения $40 при лимите PRO $50 — зонд фазы), но закрывается по форме,
а не по достижимости.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import db_adapter as db
import plan_source
from cost.predictor import CostPredictor

FREE = "free"
PRO = "pro"

CONFIRMATION_REQUIRED = plan_source.CONFIRMATION_REQUIRED
GATE_UNAVAILABLE = plan_source.GATE_UNAVAILABLE

_FORCED_VERDICT = {
    "decision": plan_source.REQUIRES_CONFIRMATION,
    "decision_category": "COST_ABOVE_TIER_LIMIT",
    "decision_reason": "стоимость выше денежного лимита тира (принудительно)",
    "estimated_cost": 41.0,
}


def _patch_tenant(monkeypatch, record: dict) -> None:
    monkeypatch.setattr(db, "get_tenant", lambda tenant_id: record)


def _patch_ledger(monkeypatch, jobs: int = 0, gpu_seconds: int = 0) -> None:
    monkeypatch.setattr(
        db,
        "get_tenant_usage_db",
        lambda tenant_id: {"total_jobs": jobs, "total_gpu_seconds": gpu_seconds},
    )


def _scheduler(gate_denied: bool = False, gpu_available: bool = False):
    from scheduler.roma_scheduler import ROMAGPUScheduler

    sched = ROMAGPUScheduler.__new__(ROMAGPUScheduler)
    sched.predictor = CostPredictor()
    sched.policy_engine = None
    sched.local_mode = "local"
    sched.gate_unavailable_reason = None
    sched.gpu_connector = SimpleNamespace(is_available=lambda: gpu_available)
    if gate_denied:
        sched.cost_gate = SimpleNamespace(
            evaluate=lambda tenant_id, payload: SimpleNamespace(
                result="denied", reason="gate denied by tenant quota"
            )
        )
    else:
        sched.cost_gate = SimpleNamespace(
            evaluate=lambda tenant_id, payload: SimpleNamespace(
                result="allowed", reason="gate allowed"
            )
        )

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


def _force_confirmation_verdict(monkeypatch, sched) -> None:
    """Форсированный вердикт того же контракта, что даёт предиктор."""
    monkeypatch.setattr(
        sched,
        "predictor",
        SimpleNamespace(predict=lambda **kwargs: dict(_FORCED_VERDICT)),
    )


def _spy_confirmation_ledger(monkeypatch) -> list:
    """Шпион на существующий леджерный путь (audit_events).

    Плюс ключ идемпотентности (`event_exists`, P3.9): записанный факт становится
    виден последующим проходам — как в реальном append-only леджере.
    """
    import audit.event_store as audit_store

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


def _job(job_id: str, tenant_id: str, gpu_required: bool, **extra):
    job = {
        "job_id": job_id,
        "task_type": "train yolov8",
        "plugin_type": "ml_training",
        "gpu_required": gpu_required,
        "command": "python3 -c 'print(1)'",
        "tenant_id": tenant_id,
    }
    job.update(extra)
    return job


# ── сцена 1: без флага — отказ, исполнения нет ──────────────────────────────


def test_forced_verdict_without_flag_is_rejected_without_execution(monkeypatch):
    """RED-якорь фазы: прежде форсированный вердикт исполнялся без подтверждения."""
    _patch_tenant(monkeypatch, {"tenant_id": "t-pro", "plan": PRO})
    _patch_ledger(monkeypatch, jobs=0, gpu_seconds=0)
    sched = _scheduler(gpu_available=True)
    _force_confirmation_verdict(monkeypatch, sched)
    events = _spy_confirmation_ledger(monkeypatch)

    route = sched.route_job(_job("j-confirm-red", "t-pro", gpu_required=False))

    assert route["status"] == "rejected", route
    assert route["reason"] == CONFIRMATION_REQUIRED, route
    assert route["hint"] == plan_source.CONFIRMATION_RESUBMIT_HINT, route
    assert "execution_target" not in route, route
    assert sched._local_calls == [], sched._local_calls
    assert events == [], events

    result = asyncio.run(sched.execute_job(_job("j-confirm-red", "t-pro", False)))
    assert result["status"] == "rejected", result
    assert result["reason"] == CONFIRMATION_REQUIRED, result
    assert sched._local_calls == [], sched._local_calls


# ── сцена 2: строгое подтверждение — проход + леджер-метка ───────────────────


def test_confirmed_true_passes_and_marks_existing_ledger(monkeypatch):
    """GREEN: confirmed: true → маршрут как прежде + факт user_confirmed в леджере."""
    _patch_tenant(monkeypatch, {"tenant_id": "t-pro", "plan": PRO})
    _patch_ledger(monkeypatch, jobs=0, gpu_seconds=0)
    sched = _scheduler(gpu_available=False)
    _force_confirmation_verdict(monkeypatch, sched)
    events = _spy_confirmation_ledger(monkeypatch)

    job = _job("j-confirm-green", "t-pro", gpu_required=False, confirmed=True)
    route = sched.route_job(job)

    assert route["status"] == "queued", route
    assert route["execution_target"] == "local", route
    assert route["user_confirmed"] is True, route
    assert len(events) == 1, events
    event = events[0]
    assert event["event_type"] == "job.user_confirmed", event
    assert event["entity_type"] == "job", event
    assert event["entity_id"] == "j-confirm-green", event
    assert event["tenant_id"] == "t-pro", event
    assert event["data"]["user_confirmed"] is True, event
    assert event["data"]["decision"] == plan_source.REQUIRES_CONFIRMATION, event

    result = asyncio.run(sched.execute_job(job))
    assert result["status"] == "success", result
    assert sched._local_calls == ["j-confirm-green"], sched._local_calls


# ── сцена 3: не-булевы формы флага — fail-closed ─────────────────────────────


def test_non_boolean_flag_forms_are_rejected(monkeypatch):
    """Строка/1/None/отсутствие подтверждением не считаются (fail-closed)."""
    _patch_tenant(monkeypatch, {"tenant_id": "t-pro", "plan": PRO})
    _patch_ledger(monkeypatch, jobs=0, gpu_seconds=0)
    events = _spy_confirmation_ledger(monkeypatch)

    for label, extra in (
        ('"yes"', {"confirmed": "yes"}),
        ("1", {"confirmed": 1}),
        ("None", {"confirmed": None}),
        ("absent", {}),
    ):
        sched = _scheduler(gpu_available=True)
        _force_confirmation_verdict(monkeypatch, sched)
        route = sched.route_job(_job(f"j-form-{label}", "t-pro", False, **extra))
        assert route["status"] == "rejected", (label, route)
        assert route["reason"] == CONFIRMATION_REQUIRED, (label, route)
        assert sched._local_calls == [], (label, sched._local_calls)

    assert events == [], events


# ── сцена 4: S3-контроль и живой путь — регрессионно зелёные ─────────────────


def test_gate_unavailable_and_live_path_unchanged(monkeypatch):
    """S3-контроль: недоступный гейт — GATE_UNAVAILABLE; живой APPROVED — как прежде."""
    _patch_tenant(monkeypatch, {"tenant_id": "t-pro", "plan": PRO})
    _patch_ledger(monkeypatch, jobs=0, gpu_seconds=0)
    events = _spy_confirmation_ledger(monkeypatch)

    blocked = _scheduler(gpu_available=True)
    blocked.gate_unavailable_reason = "decision gate unavailable (forced)"
    route = blocked.route_job(_job("j-s3", "t-pro", False, confirmed=True))
    assert route["status"] == "rejected", route
    assert route["reason"] == GATE_UNAVAILABLE, route
    assert blocked._local_calls == [], blocked._local_calls

    live = _scheduler(gpu_available=False)
    live_route = live.route_job(_job("j-live", "t-pro", False))
    assert live_route["status"] == "queued", live_route
    assert live_route["user_confirmed"] is False, live_route
    assert events == [], events


# ── сцена 5: леджер подтверждения недоступен — исполнения нет ────────────────


def test_confirmation_ledger_down_blocks_execution(monkeypatch):
    """Подтверждение обязано быть записанным фактом: леджер мёртв → GATE_UNAVAILABLE."""
    import audit.event_store as audit_store

    _patch_tenant(monkeypatch, {"tenant_id": "t-pro", "plan": PRO})
    _patch_ledger(monkeypatch, jobs=0, gpu_seconds=0)
    sched = _scheduler(gpu_available=True)
    _force_confirmation_verdict(monkeypatch, sched)

    def boom(*args, **kwargs):
        raise RuntimeError("confirmation ledger down (forced)")

    monkeypatch.setattr(audit_store, "write_event", boom)

    route = sched.route_job(_job("j-ledger-down", "t-pro", False, confirmed=True))

    assert route["status"] == "rejected", route
    assert route["reason"] == GATE_UNAVAILABLE, route
    assert sched._local_calls == [], sched._local_calls


# ── сцена 6: контракт потребителя (submit/execute_job честен) ────────────────


def test_executor_submit_honours_confirmation_contract(monkeypatch):
    """submit без флага — отказ без исполнения; с флагом — исполнение состоялось."""
    from scheduler.roma_scheduler import ROMAJobExecutor

    _patch_tenant(monkeypatch, {"tenant_id": "t-pro", "plan": PRO})
    _patch_ledger(monkeypatch, jobs=0, gpu_seconds=0)
    _spy_confirmation_ledger(monkeypatch)

    async def scenario(confirmed: bool):
        executor = ROMAJobExecutor.__new__(ROMAJobExecutor)
        executor.results = {}
        executor._job_ownership = {}
        sched = _scheduler(gpu_available=False)
        _force_confirmation_verdict(monkeypatch, sched)
        executor.scheduler = sched
        result = await executor.submit(
            _job(f"j-sub-{confirmed}", "t-pro", False, confirmed=confirmed)
        )
        return result, sched

    rejected, sched_rejected = asyncio.run(scenario(False))
    assert rejected["status"] == "rejected", rejected
    assert rejected["reason"] == CONFIRMATION_REQUIRED, rejected
    assert sched_rejected._local_calls == [], sched_rejected._local_calls

    executed, sched_executed = asyncio.run(scenario(True))
    assert executed["status"] == "success", executed
    assert sched_executed._local_calls == ["j-sub-True"], sched_executed._local_calls
