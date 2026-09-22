"""A-6: негативы проверки полноты набора — «набор нельзя тихо сузить».

Доктрина: детектор без сработавшего негатива считается несуществующим. Каждый негатив
здесь предъявляет ПОЛОМКУ, при которой проверка обязана отказать:
  * прогон, проходивший прежний «зелёный» порог 244 — раньше молчал, теперь падает;
  * РЕАЛЬНОЕ сужение прогона (--deselect двух файлов), при котором счётчик всё ещё >= 244 —
    обязан ронять проверку, иначе порог маскирует исчезновение тестов;
  * объявленное число тестов не равно записанному (junit врёт) — отказ;
  * нечитаемый junit и отсутствующий манифест канона — отказ (fail-closed);
  * обратная сборка механизма: гейт и CI-обёртка обязаны звать ОДНУ проверку
    (два порога на одну величину расходятся — так дефект и появился).

Живые данные не затрагиваются: junit-файлы синтетические либо записаны во временный
каталог pytest; PostgreSQL вложенным прогоном не загрязняется (conftest пиннит тестовую БД).
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

pytestmark = pytest.mark.ops

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "scripts" / "ci_run_completeness.py"
MANIFEST = REPO_ROOT / ".ci" / "run-completeness.json"
GATE = REPO_ROOT / "ci" / "coverage_gate.sh"
SMOKE_WRAPPER = REPO_ROOT / "scripts" / "ci_junit_smoke.py"
NESTED_ENV = "ROMA_NESTED_COMPLETENESS_RUN"

LEGACY_THRESHOLD = 244  # прежний порог: исчезновение 19 тестов оставалось «зелёным»


def _canon() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["canon"]


def _write_junit(
    path: pathlib.Path,
    *,
    passed: int = 0,
    failures: int = 0,
    errors: int = 0,
    skipped: int = 0,
    declared: int | None = None,
    elements: int | None = None,
) -> pathlib.Path:
    """Синтетический junit: `declared` (атрибут tests) можно развести с числом элементов."""
    total_elements = (
        elements if elements is not None else passed + failures + errors + skipped
    )
    cases = []
    for index in range(total_elements):
        if index < failures:
            outcome = '<failure message="synthetic" />'
        elif index < failures + errors:
            outcome = '<error message="synthetic" />'
        elif index < failures + errors + skipped:
            outcome = '<skipped message="synthetic" />'
        else:
            outcome = ""
        cases.append(
            f'<testcase classname="synthetic.Case" name="test_{index}">{outcome}</testcase>'
        )
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
        f'<testsuite name="pytest" errors="{errors}" failures="{failures}" '
        f'skipped="{skipped}" tests="{declared if declared is not None else total_elements}" '
        f'time="0.0">{"".join(cases)}</testsuite></testsuites>',
        encoding="utf-8",
    )
    return path


def _run_checker(
    *,
    junit: pathlib.Path | None = None,
    manifest: pathlib.Path = MANIFEST,
    mode: str = "repo-wide",
) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(CHECKER)]
    if junit is not None:
        cmd += ["--junit", str(junit)]
    cmd += ["--manifest", str(manifest), "--mode", mode]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT)


def _in_ci() -> bool:
    """Признак среды — те же переменные, что видит проверка (своих флагов не вводим)."""
    for var in ("CI", "GITHUB_ACTIONS"):
        raw = (os.environ.get(var) or "").strip().lower()
        if raw and raw not in ("0", "false", "no"):
            return True
    return False


def _profile() -> dict:
    """Профиль среды из канона: ожидания объявлены поимённо, а не наследуются машиной."""
    profiles = json.loads(MANIFEST.read_text(encoding="utf-8"))["profiles"]
    return profiles["ci" if _in_ci() else "node"]


def _write_named_junit(
    path: pathlib.Path,
    profile: dict,
    *,
    drop: str | None = None,
    extra: str | None = None,
) -> pathlib.Path:
    """Синтетический junit в форме профиля: passed + ПОИМЁННЫЕ admission-скипы."""
    entries = [
        entry["test"] for entry in profile["named_admissions"] if entry["test"] != drop
    ]
    if extra is not None:
        entries.append(extra)
    cases = [
        f'<testcase classname="synthetic.Case" name="test_{index}" />'
        for index in range(profile["passed"])
    ]
    for entry in entries:
        module, name = entry.split("::")
        classname = module[:-3].replace("/", ".")
        cases.append(
            f'<testcase classname="{classname}" name="{name}">'
            '<skipped message="issue: T-1 · expiry: 2026-12-31 · синтетика профиля" />'
            "</testcase>"
        )
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
        f'<testsuite name="pytest" errors="0" failures="0" '
        f'skipped="{len(entries)}" tests="{len(cases)}" time="0.0">'
        f'{"".join(cases)}</testsuite></testsuites>',
        encoding="utf-8",
    )
    return path


def test_checker_accepts_run_matching_profile(tmp_path: pathlib.Path) -> None:
    """G-CANON-ENV-PARITY, позитив: числа профиля среды + поимённые admission → зелёный.

    Печать обязана показывать профиль: собранное = passed профиля + admission профиля.
    """
    profile = _profile()
    junit = _write_named_junit(tmp_path / "profile.xml", profile)
    result = _run_checker(junit=junit)
    assert result.returncode == 0, result.stderr
    assert "RUN COMPLETENESS: PASSED" in result.stdout
    assert f"CANON         : {MANIFEST}" in result.stdout
    assert f"({profile['label']} profile)" in result.stdout, result.stdout
    assert (
        f"{_canon()['collected']} = {profile['passed']} + {profile['admission_skips']}"
        in result.stdout
    ), result.stdout


def test_checker_fails_closed_on_unknown_environment(tmp_path: pathlib.Path) -> None:
    """G-CANON-ENV-PARITY, негатив 1: чужой профиль вместо своего → отказ (fail-closed).

    «Зелёный» не имеет права опираться на числа другой машины: если профиля под текущие
    признаки среды в каноне нет, проверка обязана отказать, а не подставить чужие ожидания.
    """
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    foreign = "ci" if not _in_ci() else "node"
    manifest["profiles"] = {"foreign": manifest["profiles"][foreign]}
    path = tmp_path / "foreign-profile.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    junit = _write_named_junit(tmp_path / "profile.xml", _profile())
    result = _run_checker(junit=junit, manifest=path)
    assert result.returncode != 0
    assert "не объявлена в профилях" in result.stderr, result.stderr


def test_checker_rejects_unnamed_admission(tmp_path: pathlib.Path) -> None:
    """G-CANON-ENV-PARITY, негатив 2: скип без поимённой записи → отказ.

    Счётчик admission слеп к подмене одного осознанного скипа другим: сверка обязана
    быть поимённой, иначе «класс скипов не изменился» ничего не значит.
    """
    junit = _write_named_junit(
        tmp_path / "unnamed.xml",
        _profile(),
        extra="tests/test_synthetic.py::test_not_declared",
    )
    result = _run_checker(junit=junit)
    assert result.returncode != 0
    assert "admission без поимённой записи" in result.stderr, result.stderr


def test_checker_rejects_missing_declared_admission(tmp_path: pathlib.Path) -> None:
    """G-CANON-ENV-PARITY, негатив 3: объявленный admission не наблюдался → отказ.

    Обратная сторона поимённой сверки: исчезновение осознанного скипа (свидетельство
    сузилось) тоже обязано ронять проверку, а не проходить молча.
    """
    profile = _profile()
    junit = _write_named_junit(
        tmp_path / "missing.xml",
        profile,
        drop=profile["named_admissions"][0]["test"],
    )
    result = _run_checker(junit=junit)
    assert result.returncode != 0
    assert "предписанный профилем" in result.stderr, result.stderr


def test_checker_rejects_legacy_threshold_run(tmp_path: pathlib.Path) -> None:
    """Прежний факт 244 теста: `collected >= 244` его пропускал — теперь отказ."""
    junit = _write_junit(tmp_path / "legacy244.xml", passed=236, skipped=8)
    result = _run_checker(junit=junit)
    assert result.returncode != 0
    assert "PASSED" not in result.stdout
    assert f"collected: {LEGACY_THRESHOLD} != канон" in result.stderr


def test_checker_rejects_narrowed_run_still_above_legacy_threshold(
    tmp_path: pathlib.Path,
) -> None:
    """РЕАЛЬНЫЙ суженный прогон: два файла сняты --deselect, счётчик всё ещё >= 244."""
    if os.environ.get(NESTED_ENV) == "1":
        pytest.fail(
            "рекурсия: тест полноты запущен внутри самого себя — --deselect не применился"
        )
    junit = tmp_path / "narrowed.xml"
    # Негатив не участвует в измерении покрытия: дочерний прогон обязан быть без
    # coverage-хуков (A1-артефакт a1_coverage.pth + COV_CORE_*), иначе результат
    # негатива подмешивается в отчёт родительского прогона.
    nested_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("COV_CORE") and key != "COVERAGE_PROCESS_START"
    }
    nested_env[NESTED_ENV] = "1"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:warnings",
            "--deselect",
            "tests/test_lease_protocol.py",
            "--deselect",
            "tests/test_run_completeness.py",
            f"--junitxml={junit}",
        ],
        cwd=REPO_ROOT,
        env=nested_env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert junit.exists(), "суженный прогон не записал junitxml"
    recorded = len(list(ET.parse(junit).getroot().iter("testcase")))
    canon = _canon()
    assert LEGACY_THRESHOLD <= recorded < canon["collected"], (
        f"сужение обязано оставлять счётчик >= {LEGACY_THRESHOLD} и < канона {canon['collected']}, "
        f"получено {recorded} — иначе негатив не про прежний порог"
    )
    result = _run_checker(junit=junit)
    assert (
        result.returncode != 0
    ), "суженный прогон выше прежнего порога обязан ронять проверку"
    # G-CI-PG-CANON: формулировка зависит от класса скипов прогона (env-skip
    # добавляет пометку), но негатив обязан называть расхождение и канон.
    assert f"collected: {recorded} != " in result.stderr, result.stderr
    assert str(canon["collected"]) in result.stderr, result.stderr


def test_checker_rejects_declared_executed_mismatch(tmp_path: pathlib.Path) -> None:
    """junit объявляет канон, но записал меньше элементов: collected != executed."""
    canon = _canon()
    junit = _write_junit(
        tmp_path / "lying_attr.xml",
        passed=canon["passed"],
        skipped=canon["skipped"],
        declared=canon["collected"],
        elements=canon["collected"] - 13,
    )
    result = _run_checker(junit=junit)
    assert result.returncode != 0
    assert "collected != executed" in result.stderr


def test_checker_rejects_unparsable_junit(tmp_path: pathlib.Path) -> None:
    junit = tmp_path / "broken.xml"
    junit.write_text(
        '<?xml version="1.0"?><testsuites><testsuite name="pytest" tests="3">',
        encoding="utf-8",
    )
    result = _run_checker(junit=junit)
    assert result.returncode != 0
    assert "не читается" in result.stderr


def test_checker_fails_closed_on_missing_manifest(tmp_path: pathlib.Path) -> None:
    canon = _canon()
    junit = _write_junit(
        tmp_path / "canon.xml", passed=canon["passed"], skipped=canon["skipped"]
    )
    result = _run_checker(junit=junit, manifest=tmp_path / "no-such-manifest.json")
    assert result.returncode != 0
    assert "манифест канона не найден" in result.stderr


def test_gate_and_ci_wrapper_use_single_checker() -> None:
    """Обратная сборка: порог не должен снова разъехаться на два места."""
    gate = GATE.read_text(encoding="utf-8")
    wrapper = SMOKE_WRAPPER.read_text(encoding="utf-8")
    assert "ci_run_completeness.py" in gate
    assert (
        "MIN_COLLECTED" not in gate and "collected>=" not in gate
    ), "в гейте снова свой порог — проверка полноты обязана быть одна"
    assert "ci_run_completeness.py" in wrapper
    assert (
        re.search(r"^\s*CANON_MIN\s*=", wrapper, re.M) is None
    ), "в CI-обёртке снова свой порог"
