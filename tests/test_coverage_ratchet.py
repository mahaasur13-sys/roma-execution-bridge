"""A-7: храповик полов покрытия — негативы «пол ниже факта» и «пол понижен».

Доктрина: у каждого детектора есть негативный тест, доказывающий, что он способен
сработать. Пол покрытия без такого теста — украшение: он печатается, но ничего не решает.

Здесь предъявляются поломки, которые обязан ловить поднятый пол:
  * синтетическое измерение ЧУТЬ НИЖЕ пола продукта роняет гейт (exit 1);
  * синтетическое измерение ЧУТЬ НИЖЕ денежного пола роняет гейт (exit 1);
  * ТО ЖЕ измерение при прежних полах (floor_history[0]) проходит — прежний порог был слеп,
    то есть негатив доказывает именно новый пол, а не «гейт вообще умеет краснеть»;
  * понижение пола ниже исторического максимума — exit 3 (храповик);
  * отсутствие floor_history — exit 3 (fail-closed: храповик нечем подтвердить);
  * контроль: измерение выше полов проходит (гейт не «красный всегда»).

Живые данные не затрагиваются: измерение синтетическое и подаётся через MEASUREMENT_DIR
(гейт не запускает pytest), thresholds берётся из временной копии через THRESHOLDS.
Числа полов читаются из рабочего файла, а не зашиты: при подъёме храповика тесты остаются
верными, а прежние полы берутся из floor_history.
"""
from __future__ import annotations

import json
import math
import os
import pathlib
import subprocess

import pytest

pytestmark = pytest.mark.ops

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "ci" / "coverage_gate.sh"
THRESHOLDS = REPO_ROOT / ".ci" / "coverage-thresholds.json"
CANON = json.loads((REPO_ROOT / ".ci" / "run-completeness.json").read_text(encoding="utf-8"))["canon"]

PRODUCT_MARGIN = 0.02  # «чуть ниже» пола продукта, п.п.
MONEY_MARGIN = 0.05  # «чуть ниже» денежного пола, п.п.


def _thresholds() -> dict:
    return json.loads(THRESHOLDS.read_text(encoding="utf-8"))


def _write_junit(path: pathlib.Path) -> pathlib.Path:
    """Синтетический junitxml, совпадающий с каноном полноты (иначе негатив уедет в A-6)."""
    cases = []
    for index in range(CANON["collected"]):
        outcome = '<skipped message="synthetic" />' if index < CANON["skipped"] else ""
        cases.append(f'<testcase classname="synthetic.Case" name="test_{index}">{outcome}</testcase>')
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
        f'<testsuite name="pytest" errors="0" failures="0" skipped="{CANON["skipped"]}" '
        f'tests="{CANON["collected"]}" time="0.0">{"".join(cases)}</testsuite></testsuites>',
        encoding="utf-8",
    )
    return path


def _synthetic_path(entry: str) -> str:
    """Путь-заглушка, попадающий РОВНО под одну запись include (иначе гейт ругается на область."""
    return entry if entry.endswith(".py") else f"{entry}/_synthetic.py"


def _coverage(path: pathlib.Path, *, money_covered: int, money_statements: int, bulk_statements: int) -> pathlib.Path:
    """Синтетическое измерение покрытия: product и money управляются раздельно.

    Продукт = (заглушки + денежный файл), деньги = ТОЛЬКО файл billing/_synthetic.py
    (единственный путь под money_scope: prefix billing/ без ledger/idempotenc в имени).
    """
    include = _thresholds()["scope"]["include"]
    money_entry, bulk_entry = "billing", "alerts"
    assert money_entry in include and bulk_entry in include, "заглушки обязаны попадать в include"

    files: dict[str, dict] = {}
    for entry in include:
        if entry == money_entry:
            covered, statements = money_covered, money_statements
        elif entry == bulk_entry:
            covered, statements = 0, bulk_statements
        else:
            covered, statements = 1, 1
        files[_synthetic_path(entry)] = {
            "summary": {
                "covered_lines": covered,
                "num_statements": statements,
                "percent_covered": 100.0 * covered / statements if statements else 100.0,
            }
        }
    total_covered = sum(v["summary"]["covered_lines"] for v in files.values())
    total_statements = sum(v["summary"]["num_statements"] for v in files.values())
    path.write_text(
        json.dumps(
            {
                "files": files,
                "totals": {
                    "covered_lines": total_covered,
                    "num_statements": total_statements,
                    "percent_covered": 100.0 * total_covered / total_statements,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _measurement(
    tmp_path: pathlib.Path,
    *,
    product: str = "below",
    money: str = "above",
) -> pathlib.Path:
    """Каталог внешнего измерения: measurement/roma_cov.json + measurement/roma_junit.xml."""
    data = _thresholds()
    global_floor, money_floor = float(data["global_floor"]), float(data["money_floor"])
    if not tmp_path.exists():
        tmp_path.mkdir(parents=True)

    money_statements = 10_000
    if money == "below":
        money_covered = int(money_floor * 100) - 5  # ниже пола на 0.05 п.п.
        product_target = global_floor + 0.5  # продукт заведомо выше — падение будет про деньги
    else:
        money_covered = int(money_floor * 100) + 5  # выше пола на 0.05 п.п.
        product_target = global_floor - PRODUCT_MARGIN if product == "below" else global_floor + 0.5

    fillers = len(_thresholds()["scope"]["include"]) - 2  # заглушки по 1/1
    covered_base, statements_base = fillers + money_covered, fillers + money_statements
    if product == "below":
        bulk_statements = math.floor(100 * covered_base / product_target) - statements_base + 1
    else:
        bulk_statements = math.floor(100 * covered_base / product_target) - statements_base - 1
    assert bulk_statements >= 1, "синтетическое измерение выродилось — проверь формулу"

    _coverage(
        tmp_path / "roma_cov.json",
        money_covered=money_covered,
        money_statements=money_statements,
        bulk_statements=bulk_statements,
    )
    _write_junit(tmp_path / "roma_junit.xml")
    product_pct = 100 * covered_base / (statements_base + bulk_statements)
    money_pct = 100 * money_covered / money_statements
    assert (product_pct < global_floor) == (product == "below"), "формула не дала нужной стороны"
    assert (money_pct < money_floor) == (money == "below"), "формула не дала нужной стороны"
    return tmp_path


def _thresholds_variant(tmp_path: pathlib.Path, name: str, *, floors: tuple[float, float] | None, history: list | None) -> pathlib.Path:
    data = _thresholds()
    if floors is not None:
        data["global_floor"], data["money_floor"] = floors
    if history is None:
        data.pop("floor_history", None)
    else:
        data["floor_history"] = history
    path = tmp_path / name
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _run_gate(*, measurement: pathlib.Path, thresholds: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "MEASUREMENT_DIR": str(measurement)}
    if thresholds is not None:
        env["THRESHOLDS"] = str(thresholds)
    return subprocess.run(
        ["bash", str(GATE)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_gate_rejects_product_just_below_new_floor(tmp_path: pathlib.Path) -> None:
    """Пол продукта обязан решать: измерение на 0.02 п.п. ниже пола — отказ."""
    floors = _thresholds()
    result = _run_gate(measurement=_measurement(tmp_path / "m", product="below", money="above"))
    assert result.returncode == 1, f"ожидался отказ гейта по полу продукта:\n{result.stdout}{result.stderr}"
    assert "COVERAGE GATE: FAILED -> TOTAL" in result.stdout
    assert f"< {floors['global_floor']}" in result.stdout
    assert "RUN COMPLETENESS: PASSED" in result.stdout, "падение не должно быть следствием проверки полноты"


def test_gate_rejects_money_just_below_new_floor(tmp_path: pathlib.Path) -> None:
    """Денежный пол обязан решать отдельно: продукт выше пола, деньги — ниже."""
    floors = _thresholds()
    result = _run_gate(measurement=_measurement(tmp_path / "m", product="above", money="below"))
    assert result.returncode == 1, f"ожидался отказ гейта по денежному полу:\n{result.stdout}{result.stderr}"
    assert "COVERAGE GATE: FAILED -> MONEY PATH" in result.stdout
    assert f"< {floors['money_floor']}" in result.stdout


def test_old_floors_were_blind_to_the_same_measurement(tmp_path: pathlib.Path) -> None:
    """Негатив про НОВЫЙ пол: прежние полы (floor_history[0]) то же измерение пропускали."""
    history = _thresholds()["floor_history"]
    assert len(history) >= 2, "нужны минимум две записи храповика: прежний и текущий пол"
    old_global, old_money = float(history[0]["global_floor"]), float(history[0]["money_floor"])
    new_global, new_money = float(history[-1]["global_floor"]), float(history[-1]["money_floor"])
    assert old_global < new_global and old_money < new_money, "храповик обязан расти, иначе тест бессмыслен"

    measurement = _measurement(tmp_path / "m", product="below", money="above")
    old = _run_gate(
        measurement=measurement,
        thresholds=_thresholds_variant(
            tmp_path, "old.json", floors=(old_global, old_money), history=[history[0]]
        ),
    )
    assert old.returncode == 0, f"прежние полы должны были пропустить это измерение:\n{old.stdout}{old.stderr}"
    assert "COVERAGE GATE: PASSED" in old.stdout
    assert "RATCHET   : OK" in old.stdout


def test_gate_refuses_floor_lowering(tmp_path: pathlib.Path) -> None:
    """Храповик: пол не понижается. Понижение ниже исторического максимума — exit 3."""
    history = _thresholds()["floor_history"]
    lowered = _thresholds_variant(
        tmp_path,
        "lowered.json",
        floors=(float(history[0]["global_floor"]), float(history[0]["money_floor"])),
        history=history,
    )
    result = _run_gate(measurement=_measurement(tmp_path / "m"), thresholds=lowered)
    assert result.returncode == 3, f"понижение пола обязано быть отклонено:\n{result.stdout}{result.stderr}"
    assert "RATCHET: FAILED -> понижение пола" in result.stderr


def test_gate_refuses_missing_floor_history(tmp_path: pathlib.Path) -> None:
    """Fail-closed: без floor_history храповик нечем подтвердить, «зелено» получать нельзя."""
    stripped = _thresholds_variant(tmp_path, "no_history.json", floors=None, history=None)
    result = _run_gate(measurement=_measurement(tmp_path / "m"), thresholds=stripped)
    assert result.returncode == 3, f"отсутствие floor_history обязано быть отказом:\n{result.stdout}{result.stderr}"
    assert "нет непустого floor_history" in result.stderr


def test_gate_accepts_measurement_above_floors(tmp_path: pathlib.Path) -> None:
    """Контроль: гейт не «красный всегда» — измерение выше полов проходит."""
    result = _run_gate(measurement=_measurement(tmp_path / "m", product="above", money="above"))
    assert result.returncode == 0, f"измерение выше полов обязано проходить:\n{result.stdout}{result.stderr}"
    assert "COVERAGE GATE: PASSED" in result.stdout
    assert "RUN COMPLETENESS: PASSED" in result.stdout
