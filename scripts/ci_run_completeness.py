#!/usr/bin/env python3
"""A-6: полнота набора тестов — «набор нельзя тихо сузить».

Зачем (класс дефекта, не инстанс): смоук-порог `collected >= 244` не отличал полный
набор от набора, из которого молча выпало 19 тестов: фильтр пути, `-k` или
`--deselect` оставляли гейт зелёным. Точное равенство канону закрывает это.

Три независимых числа берутся из ОДНОГО артефакта (junitxml):
  * declared — сумма атрибутов tests у <testsuite>: сколько pytest объявил собранными;
  * recorded — число элементов <testcase> в XML: сколько реально записано;
  * outcomes — passed + failures + errors + skipped, посчитанные по элементам.
Расхождение любой пары → FAIL: молчаливый deselect виден как declared < канона,
«атрибут врёт» — как declared ≠ recorded, потеря исхода — как recorded ≠ outcomes.

Канон (точные числа последнего repo-wide прогона) — `.ci/run-completeness.json`.
Отличие от канона → FAIL, пока числа не переписаны ЯВНЫМ диффом манифеста:
порог никогда не понижается сам по себе.

G-CI-PG-CANON: скипы делятся на ДВА класса, различаемых машинно по причине в junitxml:
  * admission — скип осознан и одинаков на всех машинах (GPU-live, плейсхолдеры
    submit-роутера): постоянная величина канона (`admission_skips`);
  * env-skip  — проверка не исполнена из-за ОТСУТСТВИЯ окружения (нет `PG_DSN`).
    Локально допустимо 0 или все PG-тесты (`env_skips_expected`); в CI (`CI` выставлен
    Actions) — строго 0, иначе «зелёный» CI врёт про неисполненный money-path.
Различение — по маркеру `env-skip:` в причине скипа (`ENV_SKIP_MARKER`, тот же
литерал объявлен в `tests/conftest.py`; совпадение проверяет политика исключений).

G-CANON-ENV-PARITY: ожидания по среде объявлены ЯВНО (`profiles` в манифесте):
плоский канон был нода-относителен, и в CI (где часть проверок исполняется, а часть
не может — нет платформенных артефактов раннера) он краснел контрактно, а не по делу.
Профиль берётся из окружения (`CI`/`GITHUB_ACTIONS` → `ci`, иначе `node`); среда, которой
нет в манифесте, — отказ (fail-closed). Admission-скипы обязаны быть записаны ПОИМЁННО
(`named_admissions`, тройка: класс · симптом · issue/expiry): скип без поимённой записи
или предписанная запись, которой не было, — отказ, а не «счётчик совпал».

Режим `--mode narrow` (локальный/негативный прогон) печатает, что канон НЕ применяется,
но расхождения declared/recorded/outcomes проверяет так же — сужение области прогона
не должно быть молчаливым даже там.

CLI:
    ci_run_completeness.py --junit /tmp/roma_junit.xml
                          [--manifest .ci/run-completeness.json]
                          [--mode repo-wide|narrow]
Коды выхода: 0 — полнота подтверждена; 3 — любой отказ (fail-closed).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
import xml.etree.ElementTree as ET

EXIT_FAIL = 3
REQUIRED_KEYS = ("collected", "passed", "failures", "errors", "skipped")
# G-CI-PG-CANON: класс env-скипа различим машинно по этому маркеру в причине.
# Литерал ОБЯЗАН совпадать с ENV_SKIP_MARKER в tests/conftest.py (проверяет политика).
ENV_SKIP_MARKER = "env-skip:pg-unavailable"
CI_ENV_VARS = ("CI", "GITHUB_ACTIONS")
# G-CANON-ENV-PARITY: профиль среды + поимённый реестр admission-скипов.
PROFILE_KEYS = ("passed", "admission_skips", "env_skips_max", "named_admissions")
ADMISSION_KEYS = ("test", "class", "symptom", "issue", "expiry")


def ci_mode() -> bool:
    """Прогон в CI: Actions выставляет CI/GITHUB_ACTIONS сам (никаких своих флагов)."""
    for var in CI_ENV_VARS:
        raw = (os.environ.get(var) or "").strip().lower()
        if raw and raw not in ("0", "false", "no"):
            return True
    return False


class CompletenessError(Exception):
    """Отказ проверки полноты: причина печатается в stderr, код выхода — 3."""


# P3.2 «канон тем же коммитом»: изменение набора тестов и изменение канона
# (.ci/run-completeness.json) обязаны ехать ОДНИМ коммитом. Канон, обновлённый
# раньше или позже самих тестов, даёт красный COVERAGE GATE (рецидив: 0c94123 →
# догоняющий коммит-паритет 05956dc). Порядок: (1) изменить тесты; (2) прогнать
# --collect-only и полный прогон; (3) вписать фактические collected/passed в
# canon и profiles тем же диффом. Гейт сработал верно — менять надо только
# процесс, а не логику проверки.


def load_manifest(path: pathlib.Path) -> tuple[dict, str]:
    if not path.exists():
        raise CompletenessError(
            f"манифест канона не найден: {path} (источник точных чисел набора)"
        )
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CompletenessError(
            f"{type(exc).__name__} в манифесте {path}: {exc}"
        ) from exc
    canon = data.get("canon")
    if not isinstance(canon, dict):
        raise CompletenessError(
            f"манифест {path}: нет объекта 'canon' с точными числами"
        )
    for key in REQUIRED_KEYS:
        value = canon.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise CompletenessError(
                f"манифест {path}: canon['{key}'] не целое число: {value!r}"
            )
    for key in ("admission_skips", "env_skips_expected"):
        value = canon.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise CompletenessError(
                f"манифест {path}: canon['{key}'] не целое число: {value!r} "
                "(класс скипов обязан быть объявлен явно — fail-closed)"
            )
    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise CompletenessError(
            f"манифест {path}: нет объекта 'profiles' — ожидания по среде обязаны быть "
            "объявлены явно (неизвестная среда = отказ, fail-closed)"
        )
    for env_name, profile in profiles.items():
        if not isinstance(profile, dict):
            raise CompletenessError(
                f"манифест {path}: profiles['{env_name}'] не объект: {profile!r}"
            )
        for key in PROFILE_KEYS:
            if key == "named_admissions":
                continue
            value = profile.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                raise CompletenessError(
                    f"манифест {path}: profiles['{env_name}']['{key}'] не целое число: {value!r}"
                )
        admissions = profile.get("named_admissions")
        if not isinstance(admissions, list):
            raise CompletenessError(
                f"манифест {path}: profiles['{env_name}']['named_admissions'] не список — "
                "поимённый реестр admission-скипов обязателен (счётчик без имён слеп)"
            )
        if len(admissions) != profile["admission_skips"]:
            raise CompletenessError(
                f"манифест {path}: profiles['{env_name}']: реестр {len(admissions)} != "
                f"admission_skips {profile['admission_skips']} (счётчик и реестр обязаны совпадать)"
            )
        for entry in admissions:
            if not isinstance(entry, dict) or any(
                not isinstance(entry.get(key), str) or not entry.get(key)
                for key in ADMISSION_KEYS
            ):
                raise CompletenessError(
                    f"манифест {path}: profiles['{env_name}']: запись admission без полной "
                    f"тройки {ADMISSION_KEYS}: {entry!r}"
                )
    return data, hashlib.sha256(raw).hexdigest()


def resolve_profile(data: dict, path: pathlib.Path) -> tuple[str, dict]:
    """G-CANON-ENV-PARITY: ожидания — из объявленного профиля среды, не из догадки.

    Неизвестная среда (нет профиля под текущие CI/локальные признаки) → отказ:
    «зелёный» не имеет права опираться на числа чужой машины.
    """
    env_name = "ci" if ci_mode() else "node"
    profiles = data.get("profiles") or {}
    profile = profiles.get(env_name)
    if not isinstance(profile, dict):
        raise CompletenessError(
            f"среда '{env_name}' не объявлена в профилях {path} — неизвестная среда: "
            f"fail-closed (объявлены: {sorted(profiles)})"
        )
    # Ярлык объявлен манифестом (печать профиля читаема из лога и из файла).
    profile["label"] = str(
        profile.get("label") or ("CI" if env_name == "ci" else env_name)
    )
    return env_name, profile


def case_id_of(case: ET.Element) -> str:
    """Идентификатор теста в форме pytest-nodeid: tests/pkg/test_mod.py::test_name."""
    classname = (case.get("classname") or "").replace(".", "/")
    name = case.get("name") or ""
    return f"{classname}.py::{name}" if classname else name


def parse_junit(path: pathlib.Path) -> tuple[int, dict]:
    if not path.exists():
        raise CompletenessError(
            f"junitxml не найден: {path} (числа обязаны быть машинными)"
        )
    if path.stat().st_size == 0:
        raise CompletenessError(f"junitxml пуст: {path} (прогон не записал результат)")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise CompletenessError(
            f"junitxml не читается ({type(exc).__name__}): {exc}"
        ) from exc

    suites = [root] if root.tag == "testsuite" else [s for s in root.iter("testsuite")]
    if not suites:
        raise CompletenessError(f"в junitxml нет ни одного <testsuite>: {path}")

    declared = 0
    for suite in suites:
        declared += int(suite.get("tests") or 0)

    outcomes = {"passed": 0, "failures": 0, "errors": 0, "skipped": 0}
    env_skips = 0
    recorded = 0
    skipped_ids: list[str] = []
    env_skipped_ids: list[str] = []
    for suite in suites:
        for case in suite.iter("testcase"):
            recorded += 1
            if case.find("failure") is not None:
                outcomes["failures"] += 1
            elif case.find("error") is not None:
                outcomes["errors"] += 1
            elif case.find("skipped") is not None:
                outcomes["skipped"] += 1
                skipped_id = case_id_of(case)
                skipped_ids.append(skipped_id)
                message = case.find("skipped").get("message") or ""
                if ENV_SKIP_MARKER in message:
                    env_skips += 1
                    env_skipped_ids.append(skipped_id)
            else:
                outcomes["passed"] += 1
    outcomes["env_skips"] = env_skips
    outcomes["admission_skips"] = outcomes["skipped"] - env_skips
    outcomes["skipped_ids"] = skipped_ids
    outcomes["env_skipped_ids"] = env_skipped_ids
    return declared, outcomes | {"recorded": recorded}


def print_block(
    *,
    junit: pathlib.Path,
    declared: int,
    observed: dict,
    mode: str,
    canon: dict,
    canon_path: pathlib.Path,
    canon_sha: str,
    profile_env: str,
    profile: dict,
) -> None:
    env_label = profile["label"]
    executed = sum(observed[k] for k in ("passed", "failures", "errors", "skipped"))
    print("=== RUN COMPLETENESS (A-6: collected == executed) ===")
    print(f"JUNIT         : {junit}")
    print(f"COLLECTED     : {declared}  (объявлено pytest в junitxml)")
    print(
        f"EXECUTED      : {observed['recorded']}  (элементов <testcase> в том же файле)"
    )
    print(
        f"OUTCOMES      : {observed['passed']} passed · {observed['failures']} failed · "
        f"{observed['errors']} errors · {observed['skipped']} skipped = {executed}"
    )
    print(f"CANON         : {canon_path}")
    print(f"             sha256={canon_sha}")
    print(
        f"             {canon['collected']} collected / {canon['passed']} passed + "
        f"{canon['skipped']} skipped (fact {canon.get('measured_on', '?')})"
    )
    print(
        f"SKIP CLASSES  : admission={observed['admission_skips']} · "
        f"env-skip={observed['env_skips']} "
        f"(CI: {'yes' if ci_mode() else 'no'} · в CI env-skip обязан быть 0)"
    )
    print(
        f"PROFILE       : {env_label} "
        f"(passed {profile['passed']} · admission {profile['admission_skips']} · "
        f"env-skip max {profile['env_skips_max']})"
    )
    print(
        f"             {declared} = {profile['passed']} + {profile['admission_skips']} "
        f"({env_label} profile)"
    )
    print(f"RUN MODE      : {mode}")
    if mode == "narrow":
        print(
            "             канон НЕ применяется (локальный/негативный прогон) — область сужена явно"
        )
    print("=" * 56)


def check(
    *,
    junit: pathlib.Path,
    manifest: pathlib.Path,
    mode: str,
) -> int:
    canon_data, canon_sha = load_manifest(manifest)
    canon = canon_data["canon"]
    profile_env, profile = resolve_profile(canon_data, manifest)
    declared, observed = parse_junit(junit)
    print_block(
        junit=junit,
        declared=declared,
        observed=observed,
        mode=mode,
        canon=canon,
        canon_path=manifest,
        canon_sha=canon_sha,
        profile_env=profile_env,
        profile=profile,
    )

    problems: list[str] = []
    executed = sum(observed[k] for k in ("passed", "failures", "errors", "skipped"))
    if declared != observed["recorded"]:
        problems.append(
            f"collected != executed: junit объявляет tests={declared}, "
            f"записано элементов={observed['recorded']}"
        )
    if observed["recorded"] != executed:
        problems.append(
            f"исходы не сходятся с записями: {executed} исходов против "
            f"{observed['recorded']} элементов (потерян результат теста)"
        )
    if mode != "narrow":
        actual = {
            "collected": declared,
            "passed": observed["passed"],
            "failures": observed["failures"],
            "errors": observed["errors"],
            "skipped": observed["skipped"],
        }
        # G-CI-PG-CANON: канон описывает ПОЛНЫЙ набор (env-skip=0). Env-скипы —
        # не изменение набора, а явно моделируемое отклонение окружения: пропуск
        # PG-проверки переносит счётчик из passed в skipped, collected неизменен.
        # Поэтому ожидание считается С УЧЁТОМ класса, а не сравнением с сырым каноном;
        # число env-скипов отдельно ограничено (CI: 0; локально: 0 или env_skips_expected).
        env_skips = observed["env_skips"]
        env_label = profile["label"]
        # G-CANON-ENV-PARITY: ожидание — из профиля среды (collected общий: набор один).
        expected = {
            "collected": canon["collected"],
            "passed": profile["passed"] - env_skips,
            "failures": canon["failures"],
            "errors": canon["errors"],
            "skipped": profile["admission_skips"] + env_skips,
        }
        for key in REQUIRED_KEYS:
            if actual[key] != expected[key]:
                if env_skips:
                    problems.append(
                        f"{key}: {actual[key]} != {expected[key]} — канон {canon[key]} "
                        f"с учётом env-skip={env_skips} (набор изменился — обнови "
                        f"{manifest.name} явным диффом)"
                    )
                elif key == "collected":
                    problems.append(
                        f"{key}: {actual[key]} != канон {canon[key]} "
                        f"(набор изменился — обнови {manifest.name} явным диффом)"
                    )
                else:
                    problems.append(
                        f"{key}: {actual[key]} != ожидание профиля {env_label} "
                        f"{expected[key]} (канон {canon[key]}) — набор изменился, "
                        f"обнови {manifest.name} явным диффом"
                    )
        if observed["admission_skips"] != profile["admission_skips"]:
            problems.append(
                f"admission-скипы: {observed['admission_skips']} != профиль {profile_env} "
                f"{profile['admission_skips']} (изменился класс осознанных скипов)"
            )
        # Поимённая сверка: счётчик без имён слеп к подмене одного осознанного скипа другим.
        named = {entry["test"]: entry for entry in profile["named_admissions"]}
        env_skip_ids = set(observed["env_skipped_ids"])
        admission_ids = [
            cid for cid in observed["skipped_ids"] if cid not in env_skip_ids
        ]
        unnamed = sorted(set(admission_ids) - set(named))
        if unnamed:
            problems.append(
                f"admission без поимённой записи в профиле {profile_env}: {unnamed} "
                "(класс скипа обязан быть объявлен поимённо с тройкой класс·симптом·срок)"
            )
        missed = sorted(set(named) - set(admission_ids))
        if missed:
            problems.append(
                f"предписанный профилем {profile_env} admission не наблюдался: {missed} "
                "(свидетельство сузилось — сверка поимённая, не только счётчиком)"
            )
        if ci_mode():
            if env_skips:
                problems.append(
                    f"в CI env-skip={env_skips} (обязано быть 0): PG-проверки не исполнены — "
                    "CI обязан поднять реальный PostgreSQL (G-CI-PG-CANON)"
                )
        elif env_skips not in (0, profile["env_skips_max"]):
            problems.append(
                f"env-skip={env_skips}: локально допустимо только 0 или "
                f"{profile['env_skips_max']} (частично неисполненный PG-набор — это "
                "не норма, а расхождение)"
            )

    if problems:
        raise CompletenessError("; ".join(problems))
    print(
        "RUN COMPLETENESS: PASSED"
        + (" (канон не применялся)" if mode == "narrow" else "")
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A-6: полнота набора тестов (fail-closed)"
    )
    parser.add_argument(
        "--junit", default="/tmp/roma_junit.xml", help="junitxml одного прогона"
    )
    parser.add_argument(
        "--manifest",
        default=str(
            pathlib.Path(__file__).resolve().parents[1]
            / ".ci"
            / "run-completeness.json"
        ),
        help="манифест канона (точные числа последнего repo-wide прогона)",
    )
    parser.add_argument("--mode", choices=("repo-wide", "narrow"), default="repo-wide")
    args = parser.parse_args(argv)

    try:
        return check(
            junit=pathlib.Path(args.junit),
            manifest=pathlib.Path(args.manifest),
            mode=args.mode,
        )
    except CompletenessError as exc:
        print(f"RUN COMPLETENESS: FAILED -> {exc}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
