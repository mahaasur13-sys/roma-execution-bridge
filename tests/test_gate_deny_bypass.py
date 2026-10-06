"""G-GATE-DENY-LOCAL-BYPASS (P3.1): отказ гейта/квоты — до выбора ветки local/gpu.

Класс дефекта (не инстанс), две двери одного метакласса «отказ не доходит до исполнения»:

  а) единственный страж `gate_allowed` стоял ВНУТРИ gpu-ветки `route_job`; у local-ветки
     стража не было вовсе, поэтому DENIED-решение гейта исполнялось локально
     (печати дознания: S1 — queued/local + EXECUTED_LOCALLY=True для вердикта
     REJECTED/QUOTA_JOBS_EXHAUSTED; S2b — то же для DENIED);
  б) вердикт предиктора разбирался только двумя кодами (UNKNOWN_TENANT,
     GATE_UNAVAILABLE): REJECTED/QUOTA_* проходили мимо стража и уходили в маршрут
     queued (печать S4: вердикт REJECTED, маршрут queued/gpu_worker).

Правка фазы: единый предикат `plan_source.is_rejection` (весь спектр + fail-closed хвост)
и страж ДО выбора ветки; внутри-веточный страж удалён как мёртвый код.

Здесь проверяются маршрутные оси класса. Числа квот (C3) и fail-open инициализации (C4)
остаются в tests/test_pricing_quota_verdict.py: эта фаза — форсирование решений,
а не повторное решение квот.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace


import db_adapter as db
import plan_source
from cost.predictor import CostPredictor
from plan_source import is_rejection

FREE = "free"
PRO = "pro"

APPROVED = plan_source.APPROVED
REJECTED = plan_source.REJECTED
GATE_UNAVAILABLE = plan_source.GATE_UNAVAILABLE
QUOTA_JOBS_EXHAUSTED = plan_source.QUOTA_JOBS_EXHAUSTED
QUOTA_JOB_ESTIMATE_EXCEEDS_PER_JOB = plan_source.QUOTA_JOB_ESTIMATE_EXCEEDS_PER_JOB
QUOTA_MONTHLY_GPU_EXHAUSTED = plan_source.QUOTA_MONTHLY_GPU_EXHAUSTED


def _patch_tenant(monkeypatch, record: dict) -> None:
    monkeypatch.setattr(db, "get_tenant", lambda tenant_id: record)


def _patch_ledger(monkeypatch, jobs: int = 0, gpu_seconds: int = 0) -> None:
    monkeypatch.setattr(
        db,
        "get_tenant_usage_db",
        lambda tenant_id: {"total_jobs": jobs, "total_gpu_seconds": gpu_seconds},
    )


def _patch_ledger_down(monkeypatch) -> None:
    def boom(tenant_id):
        raise RuntimeError("usage ledger down (forced)")

    monkeypatch.setattr(db, "get_tenant_usage_db", boom)


def _scheduler(gate_denied: bool = False, gpu_available: bool = False):
    """Планировщик без сайд-эффектов конструктора + шпион на локальное исполнение."""
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


def _job(
    job_id: str, tenant_id: str, gpu_required: bool, plugin_type: str = "inference"
):
    return {
        "job_id": job_id,
        "task_type": "batch inference",
        "plugin_type": plugin_type,
        "gpu_required": gpu_required,
        "command": "python3 -c 'print(1)'",
        "tenant_id": tenant_id,
    }


# ── ось 1: вердикт-отказ предиктора доходит до обеих веток ───────────────────


def test_quota_rejected_verdict_blocks_local_branch(monkeypatch):
    """RED→GREEN (S1): REJECTED/QUOTA_JOBS_EXHAUSTED + gpu_required=False.

    Было: маршрут queued/local, локальное исполнение состоялось. Стало: rejected
    ДО выбора ветки, локальное исполнение не вызвано.
    """
    _patch_tenant(monkeypatch, {"tenant_id": "t-free", "plan": FREE})
    _patch_ledger(monkeypatch, jobs=50, gpu_seconds=0)
    sched = _scheduler(gpu_available=False)

    route = sched.route_job(_job("j-local", "t-free", gpu_required=False))

    assert route["status"] == "rejected", route
    assert route["reason"] == QUOTA_JOBS_EXHAUSTED, route
    assert "execution_target" not in route, route
    assert sched._local_calls == [], sched._local_calls

    result = asyncio.run(
        sched.execute_job(_job("j-local", "t-free", gpu_required=False))
    )
    assert result["status"] == "rejected", result
    assert sched._local_calls == [], sched._local_calls


def test_quota_rejected_verdict_blocks_gpu_branch(monkeypatch):
    """RED→GREEN (S4): вердикт REJECTED при доступном GPU не становится gpu_worker."""
    _patch_tenant(monkeypatch, {"tenant_id": "t-free", "plan": FREE})
    _patch_ledger(monkeypatch, jobs=0, gpu_seconds=0)
    sched = _scheduler(gpu_available=True)

    route = sched.route_job(
        _job("j-gpu", "t-free", gpu_required=True, plugin_type="ml_training")
    )

    assert route["status"] == "rejected", route
    assert route["reason"] == QUOTA_JOB_ESTIMATE_EXCEEDS_PER_JOB, route
    assert "execution_target" not in route, route


def test_monthly_gpu_rejected_verdict_blocks_before_branch(monkeypatch):
    """REJECTED/QUOTA_MONTHLY_GPU_EXHAUSTED — тоже отказ до ветки, а не маршрут."""
    _patch_tenant(monkeypatch, {"tenant_id": "t-free", "plan": FREE})
    _patch_ledger(monkeypatch, jobs=1, gpu_seconds=179_000)
    sched = _scheduler(gpu_available=True)

    route = sched.route_job(_job("j-month", "t-free", gpu_required=True))

    assert route["status"] == "rejected", route
    assert route["reason"] == QUOTA_MONTHLY_GPU_EXHAUSTED, route


def test_unknown_verdict_code_is_fail_closed(monkeypatch):
    """Fail-closed хвост на маршруте: незнакомая форма вердикта — отказ, не allow."""
    _patch_tenant(monkeypatch, {"tenant_id": "t-free", "plan": FREE})
    _patch_ledger(monkeypatch, jobs=0, gpu_seconds=0)
    sched = _scheduler(gpu_available=True)

    monkeypatch.setattr(
        sched.predictor,
        "predict",
        lambda **kw: {"decision": "SOME_FUTURE_VERDICT", "decision_reason": "future"},
    )

    route = sched.route_job(_job("j-future", "t-free", gpu_required=False))

    assert route["status"] == "rejected", route
    assert route["reason"] == "SOME_FUTURE_VERDICT", route


# ── ось 2: решение гейта — страж до ветки, локальный обход закрыт ────────────


def test_gate_denied_blocks_local_branch(monkeypatch):
    """RED→GREEN (S2b): DENIED при gpu_required=False.

    Было: queued/local + локальное исполнение (у local-ветки стража не было).
    Стало: rejected, исполнения нет.
    """
    _patch_tenant(monkeypatch, {"tenant_id": "t-pro", "plan": PRO})
    _patch_ledger(monkeypatch, jobs=0, gpu_seconds=0)
    sched = _scheduler(gate_denied=True, gpu_available=False)

    route = sched.route_job(_job("j-deny-local", "t-pro", gpu_required=False))

    assert route["status"] == "rejected", route
    assert route["reason"] == "gate denied by tenant quota", route
    assert sched._local_calls == [], sched._local_calls

    result = asyncio.run(
        sched.execute_job(_job("j-deny-local", "t-pro", gpu_required=False))
    )
    assert result["status"] == "rejected", result
    assert sched._local_calls == [], sched._local_calls


def test_gate_denied_blocks_gpu_branch_unchanged(monkeypatch):
    """S2a — прежнее поведение (страж в gpu-ветке): rejected, семантика та же."""
    _patch_tenant(monkeypatch, {"tenant_id": "t-pro", "plan": PRO})
    _patch_ledger(monkeypatch, jobs=0, gpu_seconds=0)
    sched = _scheduler(gate_denied=True, gpu_available=True)

    route = sched.route_job(_job("j-deny-gpu", "t-pro", gpu_required=True))

    assert route["status"] == "rejected", route
    assert route["reason"] == "gate denied by tenant quota", route


def test_approved_within_quota_still_takes_local_path(monkeypatch):
    """Живой путь не сломан: APPROVED в пределах квоты по-прежнему исполняется."""
    _patch_tenant(monkeypatch, {"tenant_id": "t-free", "plan": FREE})
    _patch_ledger(monkeypatch, jobs=1, gpu_seconds=100)
    sched = _scheduler(gpu_available=False)

    route = sched.route_job(_job("j-live", "t-free", gpu_required=False))

    assert route["status"] == "queued", route
    assert route["execution_target"] == "local", route
    assert route["gate_decision"] == "allowed", route

    result = asyncio.run(
        sched.execute_job(_job("j-live", "t-free", gpu_required=False))
    )
    assert result["status"] == "success", result
    assert sched._local_calls == ["j-live"], sched._local_calls


def test_requires_confirmation_contract_guard(monkeypatch):
    """REQUIRES_CONFIRMATION — разрешающая форма спектра (не член отказов), но с P3.7
    маршрут требует подтверждения: без строгого флага — rejected/CONFIRMATION_REQUIRED.

    Прежний контракт («роутится как прежде без подтверждения») закрыт решением
    владельца B — G-CONFIRM-PASSTHROUGH-SCHED, tests/test_confirm_passthrough_sched.py.
    """
    _patch_tenant(monkeypatch, {"tenant_id": "t-pro", "plan": PRO})
    _patch_ledger(monkeypatch, jobs=0, gpu_seconds=0)
    sched = _scheduler(gpu_available=False)

    monkeypatch.setattr(
        sched.predictor,
        "predict",
        lambda **kw: {
            "decision": "REQUIRES_CONFIRMATION",
            "decision_category": "COST_ABOVE_TIER_LIMIT",
            "decision_reason": "стоимость выше денежного лимита тира",
            "estimated_cost": 41.0,
        },
    )

    route = sched.route_job(_job("j-confirm", "t-pro", gpu_required=False))

    assert is_rejection("REQUIRES_CONFIRMATION") is False, route
    assert route["status"] == "rejected", route
    assert route["reason"] == plan_source.CONFIRMATION_REQUIRED, route
    assert "execution_target" not in route, route
    assert sched._local_calls == [], sched._local_calls


# ── ось 3: предикат отказа — спектр и fail-closed хвост ─────────────────────


def test_is_rejection_covers_declared_family():
    """Весь объявленный спектр отказов — отказ, а не «почти зелёный»."""
    for code in (
        plan_source.UNKNOWN_TENANT,
        GATE_UNAVAILABLE,
        REJECTED,
        QUOTA_JOBS_EXHAUSTED,
        QUOTA_JOB_ESTIMATE_EXCEEDS_PER_JOB,
        QUOTA_MONTHLY_GPU_EXHAUSTED,
    ):
        assert is_rejection(code) is True, code

    # категория тоже участвует: REJECTED + известная категория
    assert is_rejection(REJECTED, QUOTA_JOBS_EXHAUSTED) is True


def test_is_rejection_fail_closed_tail():
    """Неизвестная форма (и пустая) — отказ; разрешающие формы — не отказ."""
    assert is_rejection("SOMETHING_NEW") is True
    assert is_rejection(None) is True
    assert is_rejection("") is True
    assert is_rejection(None, "QUOTA_JOBS_EXHAUSTED") is True

    for allowed in (APPROVED, "REQUIRES_CONFIRMATION", "allowed"):
        assert is_rejection(allowed) is False, allowed


# ── ось 4: контракт потребителей маршрута ───────────────────────────────────


def test_executor_submit_returns_rejection_without_execution(monkeypatch):
    """Потребитель route_job (ROMAJobExecutor.submit) честен по status=='rejected'."""
    from scheduler.roma_scheduler import ROMAJobExecutor

    executor = ROMAJobExecutor.__new__(ROMAJobExecutor)
    executor.results = {}
    executor._job_ownership = {}
    executor.scheduler = SimpleNamespace(
        route_job=lambda job: {"status": "rejected", "reason": GATE_UNAVAILABLE}
    )
    executed: list = []

    async def boom(job):
        executed.append(job)

    executor.scheduler.execute_job = boom

    result = asyncio.run(
        executor.submit(_job("j-submit", "t-free", gpu_required=False))
    )

    assert result["status"] == "rejected", result
    assert executed == [], executed


def test_ledger_down_blocks_both_branches_before_execution(monkeypatch):
    """Якорь C4: недоступность счётчиков — GATE_UNAVAILABLE и ни одного исполнения."""
    _patch_tenant(monkeypatch, {"tenant_id": "t-free", "plan": FREE})
    _patch_ledger_down(monkeypatch)
    sched = _scheduler(gpu_available=True)

    for gpu_required in (True, False):
        route = sched.route_job(
            _job(f"j-fc-{gpu_required}", "t-free", gpu_required=gpu_required)
        )
        assert route["status"] == "rejected", route
        assert route["reason"] == GATE_UNAVAILABLE, route
        assert "execution_target" not in route, route

    assert sched._local_calls == [], sched._local_calls
