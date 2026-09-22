"""G-PRICING-TIER-PATH (P3 PRICING-INTEGRITY): тариф клиента — из его записи, и только он.

Класс дефекта (не инстанс): тариф брался из payload (`job["tenant_tier"]`, дефолт "FREE"),
а `tier_map.get(tenant_tier, PricingTier.FREE)` молча подставлял FREE для любого ярлыка,
которого нет в карте — включая законные планы в нижнем регистре из записи клиента
(`free`/`pro`/`enterprise`). Итог: платный клиент получал цену free-тарифа (0.0) и
free-вердикт, которые к его плану не относятся.

Здесь три оси проверки:
  1) тариф разрешается из `db.get_tenant(tenant_id)["plan"]` и попадает и в цену, и в риск/вердикт;
  2) payload-тариф не авторитетен: он игнорируется с логом попытки;
  3) клиента без записи (или план вне явной карты) — отдельный отказ, а не silent-FREE.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

import db_adapter as db
from billing.pricing_engine import PricingTier
from cost.predictor import (
    UNKNOWN_PLAN,
    UNKNOWN_TENANT,
    CostPredictor,
    tenant_plan,
    tier_from_plan,
)


def _record(plan: str, tenant_id: str = "t-1") -> dict:
    return {"tenant_id": tenant_id, "plan": plan}


def _patch_tenant(monkeypatch, record) -> None:
    monkeypatch.setattr(db, "get_tenant", lambda tenant_id: record)


def _bare_scheduler():
    """Сборка планировщика без сайд-эффектов конструктора (GPU-коннектор не поднимается).

    Гейт подменён разрешающей заглушкой с фактическим контрактом `evaluate(tenant_id,
    payload) -> GateDecision`: цена здесь проверяется на тарифном пути, а не на квоте.
    """
    from scheduler.roma_scheduler import ROMAGPUScheduler

    sched = ROMAGPUScheduler.__new__(ROMAGPUScheduler)
    sched.predictor = CostPredictor()
    sched.policy_engine = None
    sched.local_mode = "local"
    sched.gpu_connector = SimpleNamespace(is_available=lambda: False)
    sched.cost_gate = SimpleNamespace(
        evaluate=lambda tenant_id, payload: SimpleNamespace(
            result="allowed", reason="quota ok"
        )
    )
    return sched


# ── ось 1: тариф из записи клиента ──────────────────────────────────────────


def test_pro_tenant_is_priced_by_his_record_not_free(monkeypatch):
    """RED→GREEN: план `pro` в записи → цена PRO; free-цена (0.0) — исторический дефект."""
    _patch_tenant(monkeypatch, _record("pro", "t-pro"))
    pred = CostPredictor().predict(
        "train yolov8", True, "ml_training", tenant_id="t-pro"
    )

    assert pred["tier"] == "pro"
    assert pred["tier_source"] == "tenant-record:pro"
    assert pred["estimated_cost"] > 0, "цена free-тарифа вместо цены плана клиента"
    assert pred["breakdown"]["tier"] == "pro"


def test_enterprise_tenant_is_not_priced_as_free(monkeypatch):
    _patch_tenant(monkeypatch, _record("enterprise", "t-ent"))
    pred = CostPredictor().predict(
        "batch inference", True, "inference", tenant_id="t-ent"
    )

    assert pred["tier"] == "enterprise"
    assert pred["estimated_cost"] > 0


def test_paid_tenant_gets_no_free_tier_risk_flags(monkeypatch):
    """Риск-оценка и вердикт строятся на разрешённом тарифе, а не на free-ярлыке."""
    _patch_tenant(monkeypatch, _record("pro", "t-pro"))
    pred = CostPredictor().predict(
        "train yolov8", True, "ml_training", tenant_id="t-pro"
    )

    assert "FREE_TIER_LIMIT_RISK" not in pred["risk_flags"], pred["risk_flags"]
    assert pred["decision"] == "APPROVED", pred["decision"]


def test_the_tenant_record_is_actually_read(monkeypatch):
    """Тариф не выводится из аргументов вызова: запись клиента обязана быть прочитана."""
    seen: list[str] = []

    def spy(tenant_id):
        seen.append(tenant_id)
        return _record("pro", tenant_id)

    monkeypatch.setattr(db, "get_tenant", spy)
    CostPredictor().predict("task", False, tenant_id="t-spy")

    assert seen == ["t-spy"]


def test_free_plan_pricing_is_unchanged(monkeypatch):
    """Free-клиент: цена не сдвинута правкой (его тариф и есть free)."""
    _patch_tenant(monkeypatch, _record("free", "t-free"))
    pred = CostPredictor().predict("task", False, tenant_id="t-free")

    assert pred["tier"] == "free"
    assert pred["estimated_cost"] == 0.0


# ── ось 2: payload-тариф не авторитетен ─────────────────────────────────────


def test_payload_tier_cannot_raise_free_customer(monkeypatch):
    """payload tenant_tier=PRO у free-клиента не поднимает тариф."""
    _patch_tenant(monkeypatch, _record("free", "t-free"))
    pred = CostPredictor().predict("task", False, tenant_id="t-free", tenant_tier="PRO")

    assert pred["tier"] == "free"
    assert pred["estimated_cost"] == 0.0


def test_payload_tier_attempt_is_logged(monkeypatch):
    """Попытка подменить тариф payload-полем видна в логе — тихой подмены нет.

    Свидетельство снимается подменой самого вызова логгера модуля, а не захватом
    через handler/caplog: в repo-wide прогоне `tests/test_gpu_integration_smoke.py`
    глушит логи глобально (`logging.disable(CRITICAL)`, без обратного включения),
    поэтому любой захват через logging-подсистему зависел бы от порядка тестов.
    """
    import cost.predictor as predictor_module

    messages: list[str] = []
    monkeypatch.setattr(
        predictor_module.logger,
        "warning",
        lambda fmt, *args: messages.append(fmt % args),
    )

    # вверх: free-клиент с payload PRO — попытка поднять тариф
    _patch_tenant(monkeypatch, _record("free", "t-free"))
    CostPredictor().predict("task", False, tenant_id="t-free", tenant_tier="PRO")
    # вниз: pro-клиент с payload FREE — попытка опустить тариф
    _patch_tenant(monkeypatch, _record("pro", "t-pro"))
    CostPredictor().predict(
        "train yolov8", True, "ml_training", tenant_id="t-pro", tenant_tier="FREE"
    )
    # дефолт вызывающего совпал с записью → это не попытка подмены: записи нет
    _patch_tenant(monkeypatch, _record("free", "t-free"))
    CostPredictor().predict("task", False, tenant_id="t-free")

    assert len(messages) == 2, messages
    assert all("игнорирован" in m for m in messages), messages


def test_payload_tier_cannot_lower_paid_customer(monkeypatch):
    """payload tenant_tier=FREE у pro-клиента не опускает тариф до free-цены."""
    _patch_tenant(monkeypatch, _record("pro", "t-pro"))
    pred = CostPredictor().predict(
        "train yolov8", True, "ml_training", tenant_id="t-pro", tenant_tier="FREE"
    )

    assert pred["tier"] == "pro"
    assert pred["estimated_cost"] > 0


# ── ось 3: отказы вместо silent-FREE ────────────────────────────────────────


def test_client_without_record_is_refused(monkeypatch):
    """Клиента нет в записях → отдельный отказ UNKNOWN_TENANT (не FREE по умолчанию)."""
    monkeypatch.setattr(db, "get_tenant", lambda tenant_id: None)
    pred = CostPredictor().predict("task", True, tenant_id="t-missing")

    assert pred["decision"] == UNKNOWN_TENANT
    assert pred["estimated_cost"] is None, "цена не считается по выдуманному тарифу"
    assert pred["tier"] is None and pred["tier_source"] is None
    assert pred["decision_reason"] == (
        "клиент не найден в записях: тариф не установлен (не FREE по умолчанию)"
    )


def test_missing_tenant_id_is_refused():
    """Нет tenant_id — нет источника тарифа: тот же отдельный отказ, не free-цена."""
    pred = CostPredictor().predict("task", True)

    assert pred["decision"] == UNKNOWN_TENANT
    assert pred["estimated_cost"] is None
    assert pred["decision_reason"]


def test_plan_outside_explicit_map_is_refused(monkeypatch):
    """План вне явной карты (напр. 'start') не превращается в FREE молча."""
    _patch_tenant(monkeypatch, _record("start", "t-start"))
    pred = CostPredictor().predict("task", False, tenant_id="t-start")

    assert pred["decision"] == UNKNOWN_PLAN
    assert pred["estimated_cost"] is None


def test_plan_to_tier_map_is_case_insensitive():
    assert tier_from_plan("PRO") is PricingTier.PRO
    assert tier_from_plan(" Pro ") is PricingTier.PRO
    assert tier_from_plan("free") is PricingTier.FREE
    assert tier_from_plan("Enterprise") is PricingTier.ENTERPRISE


def test_tenant_plan_requires_a_tenant_id(monkeypatch):
    _patch_tenant(monkeypatch, _record("pro", "t-x"))

    assert tenant_plan("t-x") == "pro"
    assert tenant_plan(None) is None
    assert tenant_plan("") is None


def test_service_channel_needs_an_explicit_admin_right(monkeypatch):
    """Служебный канал: тариф задаётся явно и подписывается источником."""
    pred = CostPredictor().predict(
        "task", False, tenant_tier="PRO", admin_override=True
    )

    assert pred["tier"] == "pro"
    assert pred["tier_source"] == "admin-override:PRO"


def test_service_channel_is_fail_closed_too():
    pred = CostPredictor().predict(
        "task", False, tenant_tier="GOLD", admin_override=True
    )

    assert pred["decision"] == UNKNOWN_PLAN
    assert pred["estimated_cost"] is None


# ── путь планировщика ───────────────────────────────────────────────────────


def test_scheduler_refuses_job_of_unknown_client(monkeypatch):
    monkeypatch.setattr(db, "get_tenant", lambda tenant_id: None)
    route = _bare_scheduler().route_job(
        {
            "job_id": "j-unknown",
            "task_type": "ml_training",
            "gpu_required": True,
            "tenant_id": "t-no-record",
        }
    )

    assert route["status"] == "rejected"
    assert route["reason"] == UNKNOWN_TENANT
    assert route["estimated_cost"] is None


def test_scheduler_prices_job_by_tenant_record(monkeypatch):
    _patch_tenant(monkeypatch, _record("pro", "t-pro"))
    route = _bare_scheduler().route_job(
        {
            "job_id": "j-pro",
            "task_type": "inference",
            "gpu_required": True,
            "tenant_id": "t-pro",
            "tenant_tier": "FREE",
        }
    )

    assert route["status"] == "queued", route
    assert route["estimated_cost"] > 0, "payload-ярлык FREE не должен задавать цену"


def test_scheduler_no_longer_feeds_payload_tier_to_predictor():
    """Регресс-защита: payload-поле tenant_tier не подаётся предиктору как тариф."""
    from scheduler.roma_scheduler import ROMAGPUScheduler

    src = inspect.getsource(ROMAGPUScheduler.route_job)

    assert "tenant_tier=job.get(" not in src
    assert "tenant_id=job.get(" in src


if __name__ == "__main__":  # pragma: no cover - ручной прогон файла
    raise SystemExit(pytest.main([__file__, "-q"]))
