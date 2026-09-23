"""G-PRICING-TIER-PATH (P3 PRICING-INTEGRITY): тарифный путь CLI — из записи клиента.

Класс дефекта (не инстанс): CLI не был импортируем (`from cost.gate import DecisionGate`
— такого имени в cost/gate.py нет), а единственная точка, где CLI называл клиента,
была строкой-литералом `tenant_id="default-tenant"`. Итог: тарифный путь CLI не
проверялся ни одним прогоном, а если бы и исполнился — считал бы цену и квоту по
выдуманному идентификатору, то есть по чужому/дефолтному тарифу.

Три оси проверки:
  1) цена CLI строится по тарифу из записи клиента (`ROMA_TENANT_ID`);
  2) клиента без записи CLI не считает: отказ UNKNOWN_TENANT, exit 1, без падения;
  3) гейт получает тот же идентификатор клиента, что и цена (литерал убран).
"""

from __future__ import annotations

import re

import pytest

import db_adapter as db
import roma_cli
from cost.gate import GateDecision, GateResult


def _record(plan: str, tenant_id: str = "t-cli") -> dict:
    return {"id": tenant_id, "tenant_id": tenant_id, "plan": plan}


def _cli(monkeypatch, tenant_id: str | None) -> roma_cli.ROMA_CLI:
    if tenant_id is None:
        monkeypatch.delenv(roma_cli.TENANT_ID_ENV, raising=False)
    else:
        monkeypatch.setenv(roma_cli.TENANT_ID_ENV, tenant_id)
    return roma_cli.ROMA_CLI()


def _cost_of(out: str) -> float:
    match = re.search(r"\"estimated_cost\": ([0-9.]+)", out)
    assert match, out
    return float(match.group(1))


def test_cli_prices_by_tenant_record_plan(monkeypatch, capsys):
    """Одна и та же задача, два клиента — цена следует плану записи, а не дефолту."""
    pro = _record("pro")
    monkeypatch.setattr(db, "get_tenant", lambda tenant_id: pro)
    cli = _cli(monkeypatch, pro["tenant_id"])

    assert cli.tenant_id == pro["tenant_id"]
    rc = cli.cmd_cost("train yolov8 on gpu")
    out_pro = capsys.readouterr().out

    assert rc == 0, out_pro
    assert "ЦЕНА НЕ УСТАНОВЛЕНА" not in out_pro

    free = _record("free", pro["tenant_id"])
    monkeypatch.setattr(db, "get_tenant", lambda tenant_id: free)
    cli_free = _cli(monkeypatch, free["tenant_id"])
    rc_free = cli_free.cmd_cost("train yolov8 on gpu")
    out_free = capsys.readouterr().out

    assert rc_free == 0, out_free
    pro_cost, free_cost = _cost_of(out_pro), _cost_of(out_free)
    assert pro_cost > 0.0, out_pro
    assert free_cost == 0.0, out_free
    assert pro_cost != free_cost, (pro_cost, free_cost)


def test_cli_refuses_without_tenant_record(monkeypatch, capsys):
    """Клиента нет в записях → отказ с кодом, а не цена 0.0 и не падение."""
    monkeypatch.setattr(db, "get_tenant", lambda tenant_id: None)
    cli = _cli(monkeypatch, "t-nowhere")

    rc = cli.cmd_cost("train yolov8 on gpu")

    out = capsys.readouterr().out
    assert rc == 1, out
    assert "UNKNOWN_TENANT" in out, out
    assert "estimated_cost" not in out, out


def test_cli_without_tenant_id_refuses_clean(monkeypatch, capsys):
    """Без ROMA_TENANT_ID цена не выдумывается: отказ и понятное сообщение."""
    cli = _cli(monkeypatch, None)

    rc = cli.cmd_cost("train yolov8 on gpu")

    out = capsys.readouterr().out
    assert rc == 1, out
    assert "UNKNOWN_TENANT" in out, out
    assert roma_cli.TENANT_ID_ENV in out, out


def test_cli_gate_gets_the_same_tenant_as_price(monkeypatch, capsys):
    """Гейт вызывается с клиентом из окружения — литерал 'default-tenant' убран."""
    record = _record("pro", "t-cli-gate")
    monkeypatch.setattr(db, "get_tenant", lambda tenant_id: record)
    seen: dict = {}

    def _evaluate(tenant_id, payload=None):
        seen["tenant_id"] = tenant_id
        seen["payload"] = payload
        return GateDecision(GateResult.ALLOWED, "quota ok", tenant_id)

    monkeypatch.setattr(
        roma_cli, "_api_post", lambda path, payload: {"job_id": "job-1"}
    )
    cli = _cli(monkeypatch, record["tenant_id"])
    cli.gate = type("G", (), {"evaluate": staticmethod(_evaluate)})()

    rc = cli.cmd_run("train yolov8 on gpu")

    out = capsys.readouterr().out
    assert rc == 0, out
    assert seen["tenant_id"] == "t-cli-gate", seen
    assert seen["tenant_id"] != "default-tenant"
    assert seen["payload"]["task"] == "train yolov8 on gpu"


def test_cli_gate_denial_stops_before_submit(monkeypatch, capsys):
    """Отказ гейта — терминален: задача не отправляется."""
    record = _record("free", "t-cli-deny")
    monkeypatch.setattr(db, "get_tenant", lambda tenant_id: record)
    submitted: list = []
    monkeypatch.setattr(
        roma_cli,
        "_api_post",
        lambda path, payload: submitted.append(path) or {"job_id": "j"},
    )
    cli = _cli(monkeypatch, record["tenant_id"])
    cli.gate = type(
        "G",
        (),
        {
            "evaluate": staticmethod(
                lambda tenant_id, payload=None: GateDecision(
                    GateResult.DENIED, "quota exceeded: 50/50 jobs", tenant_id
                )
            )
        },
    )()

    rc = cli.cmd_run("train yolov8 on gpu")

    out = capsys.readouterr().out
    assert rc == 1, out
    assert "ОТКЛОНЕНО" in out, out
    assert submitted == []


def test_cli_module_imports_with_real_gate_contract():
    """CLI обязан импортироваться: имя DecisionGate в cost/gate.py не существует."""
    assert issubclass(roma_cli.EnterpriseDecisionGate, object)
    assert roma_cli.GateResult.DENIED.value == "denied"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
