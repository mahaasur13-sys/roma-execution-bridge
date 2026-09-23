"""C3/C4 (P3 PRICING-INTEGRITY): вердикт FREE-тира — по квоте; авария гейта — fail-closed.

Классы дефектов, закрываемые здесь:

  * G-QUOTA-SOURCE-FRAGMENTED — четыре таблицы квот дрейфовали независимо; теперь
    единственный источник `config/plans.json` (через `plan_source`), тир описан
    двумя полями `jobs_per_month` + `gpu_s_per_job`, месячный ресурс — производное.
  * G-FREE-DECISION-HEURISTIC — free-ставки обнулены, поэтому денежный вердикт для
    FREE всегда строился на нуле, а решение принималось эвристикой длительности
    (`FREE_TIER_LIMIT_RISK`). Теперь FREE решается двумерным предикатом по квоте
    (джобы/мес и GPU-секунды) с обязательным структурным основанием.
  * G-GATE-FAILOPEN — авария инициализации гейта или недоступность счётчиков
    выключали проверку молча (`allowed=True`). Теперь это отдельный код
    `GATE_UNAVAILABLE`, блокирующий исполнение и в CLI, и в планировщике.
"""

from __future__ import annotations

import pytest

import db_adapter as db
import plan_source
from plan_source import (
    GATE_UNAVAILABLE,
    QUOTA_JOB_ESTIMATE_EXCEEDS_PER_JOB,
    QUOTA_JOBS_EXHAUSTED,
    QUOTA_MONTHLY_GPU_EXHAUSTED,
)

FREE = "free"


def _record(plan: str, tenant_id: str = "t-1") -> dict:
    return {"tenant_id": tenant_id, "plan": plan}


def _patch_tenant(monkeypatch, record) -> None:
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


# ── ось 1: вердикт FREE решается квотой, а не деньгами и не длительностью ─────


def test_free_within_quota_is_approved_with_structural_basis(monkeypatch):
    """RED→GREEN: free-джоб в пределах ОБОИХ измерений — APPROVED с основанием."""
    _patch_tenant(monkeypatch, _record(FREE, "t-free"))
    _patch_ledger(monkeypatch, jobs=0, gpu_seconds=0)

    from cost.predictor import CostPredictor

    pred = CostPredictor().predict(
        "batch inference", True, "inference", tenant_id="t-free"
    )

    assert pred["decision"] == "APPROVED", pred
    assert pred["decision_category"] is None
    basis = pred["decision_basis"]
    assert basis["tier"] == FREE
    assert basis["jobs_used"] == 0 and basis["jobs_limit"] == 50
    assert basis["estimate_gpu_s"] <= basis["gpu_s_per_job"]
    assert basis["monthly_limit_gpu_s"] == 50 * 3600 == 180_000
    assert "proximity_pct" in basis
    assert str(basis["jobs_limit"]) in pred["decision_reason"]


def test_free_over_per_job_quota_is_rejected_with_distance(monkeypatch):
    """RED→GREEN: смета джоба сверх per-job квоты — REJECTED с расстоянием до предела."""
    _patch_tenant(monkeypatch, _record(FREE, "t-free"))
    _patch_ledger(monkeypatch, jobs=0, gpu_seconds=0)

    from cost.predictor import CostPredictor

    # ml_training: 7200 s × 1.1 (gpu) = 7920 GPU-s > 3600 per-job
    pred = CostPredictor().predict(
        "train yolov8", True, "ml_training", tenant_id="t-free"
    )

    assert pred["decision"] == "REJECTED", pred
    assert pred["decision_category"] == QUOTA_JOB_ESTIMATE_EXCEEDS_PER_JOB
    assert pred["decision_basis"]["distance"]["per_job_over_by_gpu_s"] == 7920 - 3600
    assert "7920" in pred["decision_reason"] and "3600" in pred["decision_reason"]


def test_free_jobs_exhausted_is_rejected(monkeypatch):
    """Исчерпание джобов/мес — REJECTED/QUOTA_JOBS_EXHAUSTED (счётчик из леджера)."""
    _patch_tenant(monkeypatch, _record(FREE, "t-free"))
    _patch_ledger(monkeypatch, jobs=50, gpu_seconds=0)

    from cost.predictor import CostPredictor

    pred = CostPredictor().predict("task", False, tenant_id="t-free")

    assert pred["decision"] == "REJECTED", pred
    assert pred["decision_category"] == QUOTA_JOBS_EXHAUSTED
    assert pred["decision_basis"]["distance"]["jobs_to_limit"] == 0


def test_free_monthly_gpu_exhausted_is_rejected(monkeypatch):
    """Исчерпание месячного GPU-ресурса — REJECTED/QUOTA_MONTHLY_GPU_EXHAUSTED."""
    _patch_tenant(monkeypatch, _record(FREE, "t-free"))
    _patch_ledger(monkeypatch, jobs=1, gpu_seconds=179_000)

    from cost.predictor import CostPredictor

    # cpu-джоб: смета GPU = 0, но потребление уже почти на производном пределе;
    # берём gpu-джоб в пределах per-job, чтобы сработало именно месячное измерение
    pred = CostPredictor().predict(
        "batch inference", True, "inference", tenant_id="t-free"
    )

    assert pred["decision"] == "REJECTED", pred
    assert pred["decision_category"] == QUOTA_MONTHLY_GPU_EXHAUSTED
    assert pred["decision_basis"]["distance"]["monthly_over_by_gpu_s"] > 0


def test_free_tier_limit_risk_flag_is_gone(monkeypatch):
    """Поправка №1: флаг FREE_TIER_LIMIT_RISK удалён целиком, а не «оставлен информационным».

    После сведения квот каждый его сценарий — превышение per-job квоты, то есть
    REJECTED, а не «риск»: флаг стал бы недостижимой декорацией.
    """
    _patch_tenant(monkeypatch, _record(FREE, "t-free"))
    _patch_ledger(monkeypatch, jobs=0, gpu_seconds=0)

    from cost.predictor import CostPredictor

    pred = CostPredictor().predict(
        "train yolov8", True, "ml_training", tenant_id="t-free"
    )
    assert "FREE_TIER_LIMIT_RISK" not in pred["risk_flags"], pred["risk_flags"]

    import inspect

    src = inspect.getsource(CostPredictor._assess_risk)
    assert 'flags.append("FREE_TIER_LIMIT_RISK")' not in src
    assert 'tier == "FREE"' not in src


def test_paid_tier_money_verdict_is_unchanged(monkeypatch):
    """Не-FREE поведение неизменно: денежный вердикт по лимиту тира."""
    _patch_tenant(monkeypatch, _record("pro", "t-pro"))

    from cost.predictor import CostPredictor

    pred = CostPredictor().predict(
        "train yolov8", True, "ml_training", tenant_id="t-pro"
    )

    assert pred["tier"] == "pro"
    assert pred["decision"] == "APPROVED", pred
    assert pred["decision_basis"]["tier_limit_usd"] == 50.0


# ── ось 2: недоступность счётчиков — fail-closed, а не молчаливый allow ───────


def test_ledger_unavailable_is_fail_closed_in_predictor(monkeypatch):
    """Счётчики недоступны → GATE_UNAVAILABLE (отдельный код), никогда не APPROVED."""
    _patch_tenant(monkeypatch, _record(FREE, "t-free"))
    _patch_ledger_down(monkeypatch)

    from cost.predictor import CostPredictor

    pred = CostPredictor().predict(
        "batch inference", True, "inference", tenant_id="t-free"
    )

    assert pred["decision"] == GATE_UNAVAILABLE, pred
    assert pred["decision"] != "APPROVED"


def test_ledger_unavailable_blocks_scheduler_before_gpu_branch(monkeypatch):
    """Блок в планировщике — до ветки gpu/local: исполнение не состоится."""
    from scheduler.roma_scheduler import ROMAGPUScheduler

    _patch_tenant(monkeypatch, _record(FREE, "t-free"))
    _patch_ledger_down(monkeypatch)

    sched = ROMAGPUScheduler.__new__(ROMAGPUScheduler)
    sched.predictor = __import__(
        "cost.predictor", fromlist=["CostPredictor"]
    ).CostPredictor()
    sched.policy_engine = None
    sched.local_mode = "local"
    sched.cost_gate = None
    sched.gpu_connector = type("C", (), {"is_available": lambda self: False})()

    route = sched.route_job(
        {
            "job_id": "j-fc",
            "task_type": "inference",
            "gpu_required": True,
            "tenant_id": "t-free",
        }
    )

    assert route["status"] == "rejected", route
    assert route["reason"] == GATE_UNAVAILABLE
    assert "execution_target" not in route


def test_ledger_unavailable_blocks_cli_submission(monkeypatch, capsys):
    """CLI тоже блокирует: ни одной отправки задачи при недоступных счётчиках."""
    import roma_cli

    _patch_tenant(monkeypatch, _record(FREE, "t-cli-fc"))
    _patch_ledger_down(monkeypatch)
    monkeypatch.setenv(roma_cli.TENANT_ID_ENV, "t-cli-fc")

    sent: list = []
    monkeypatch.setattr(
        roma_cli, "_api_post", lambda path, payload: sent.append(payload)
    )

    cli = roma_cli.ROMA_CLI()
    rc = cli.cmd_run("batch inference on gpu")

    out = capsys.readouterr().out
    assert rc == 1, out
    assert GATE_UNAVAILABLE in out, out
    assert sent == [], sent


# ── ось 3: C4 — авария инициализации гейта ───────────────────────────────────


def test_gate_init_failure_is_fail_closed(monkeypatch):
    """RED→GREEN: авария init давала allowed=True ('cost gate disabled'); теперь блок."""
    import cost.gate as gate_mod
    import scheduler.roma_scheduler as sched_mod

    _patch_tenant(monkeypatch, {"tenant_id": "t-sched", "plan": "pro"})

    class Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("forced init failure")

    monkeypatch.setattr(gate_mod, "EnterpriseDecisionGate", Boom)
    monkeypatch.setattr(sched_mod, "DecisionGate", Boom)

    sched = sched_mod.ROMAGPUScheduler()
    route = sched.route_job(
        {
            "job_id": "j-init",
            "task_type": "inference",
            "gpu_required": False,
            "tenant_id": "t-sched",
        }
    )

    assert route["status"] == "rejected", route
    assert route["reason"] == GATE_UNAVAILABLE
    assert "forced init failure" in route["detail"]


def test_normal_gate_init_is_unchanged(monkeypatch):
    """Негативно-регрессионный: штатная инициализация — прежнее поведение."""
    import scheduler.roma_scheduler as sched_mod

    _patch_tenant(monkeypatch, {"tenant_id": "t-sched", "plan": "pro"})

    sched = sched_mod.ROMAGPUScheduler()

    assert sched.cost_gate is not None
    assert sched.gate_unavailable_reason is None

    route = sched.route_job(
        {
            "job_id": "j-ok",
            "task_type": "inference",
            "gpu_required": False,
            "tenant_id": "t-sched",
        }
    )

    assert route["status"] == "queued", route
    assert route["execution_target"] == "local"


# ── ось 4: единственный источник квот ────────────────────────────────────────


def test_the_four_named_sources_derive_from_one_file():
    """Четыре подписанных источника читают один и тот же источник, а не свои таблицы."""
    from auth.quota_engine import QuotaEngine
    from tenancy.manager import TenantManager

    plans = plan_source.load_plans()
    assert set(plans) >= {"free", "pro", "enterprise"}

    limits_free = plan_source.plan_limits(FREE)
    assert plans[FREE]["jobs_per_month"] == limits_free.jobs_per_month == 50
    assert plans[FREE]["gpu_s_per_job"] == limits_free.gpu_s_per_job == 3600
    assert limits_free.gpu_s_per_month == 180_000

    assert QuotaEngine.PLAN_QUOTAS["FREE"] == limits_free.gpu_s_per_month
    assert (
        QuotaEngine.PLAN_QUOTAS["PRO"] == plan_source.plan_limits("pro").gpu_s_per_month
    )
    assert QuotaEngine.PLAN_QUOTAS["ENTERPRISE"] == plan_source.UNLIMITED

    tm = TenantManager()
    assert (
        tm.get_tenant_info("tenant-free")["quota_gpu_seconds"]
        == limits_free.gpu_s_per_job
    )
    assert tm.get_tenant_info("tenant-pro")["quota_gpu_seconds"] == 3600

    db_plans = db._load_plans()
    assert db_plans["free"]["max_jobs"] == 50
    assert db_plans["free"]["max_jobs_per_month"] == 50
    assert db_plans["free"]["gpu_s_per_job"] == 3600
    assert db_plans["free"]["gpu_s_per_month"] == 180_000
    assert db_plans["free"]["max_gpu_hours"] == 50

    assert not hasattr(db, "_PLAN_DEFAULTS"), "вторая таблица литералов не удалена"


def test_unlimited_means_unlimited_everywhere(monkeypatch):
    """-1 = безлимит: enterprise не запрещает крупный запрос ни в одной из точек."""
    from auth.quota_engine import QuotaEngine
    from tenancy.manager import TenantManager

    engine = QuotaEngine()
    ok, msg = engine.check_quota("t-ent", 10_000_000, "ENTERPRISE")
    assert ok is True, msg
    headers = engine.quota_headers("t-ent", "ENTERPRISE")
    assert headers["X-ROMA-Quota-Limit"] == "-1"
    assert headers["X-ROMA-Quota-Remaining"] == "-1"

    verdict = TenantManager().enforce_quota("tenant-enterprise", 10_000_000)
    assert verdict["allowed"] is True, verdict
    assert verdict["remaining"] == -1

    _patch_tenant(monkeypatch, _record("enterprise", "t-ent"))
    _patch_ledger(monkeypatch, jobs=10_000, gpu_seconds=10**9)

    from cost.predictor import CostPredictor

    pred = CostPredictor().predict(
        "train yolov8", True, "ml_training", tenant_id="t-ent"
    )
    assert pred["decision"] == "APPROVED", pred
    assert pred["decision_basis"]["tier_limit_usd"] == 500.0


if __name__ == "__main__":  # pragma: no cover - ручной прогон файла
    raise SystemExit(pytest.main([__file__, "-q"]))
