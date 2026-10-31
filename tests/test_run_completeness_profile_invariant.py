"""Guard: каждый профиль канона описывает ПОЛНЫЙ набор (до вычитания env-skip).

scripts/ci_run_completeness.py:316-322 ожидает passed = profile.passed - env_skips и
collected = canon.collected, поэтому profile.passed + admission_skips обязаны == canon.collected.
Если профиль хранит passed с уже вычтенным env-skip, проверка вычтет его второй раз.
"""

import json
import pathlib

import pytest

MANIFEST = pathlib.Path(__file__).resolve().parents[1] / ".ci" / "run-completeness.json"
DATA = json.loads(MANIFEST.read_text(encoding="utf-8"))


@pytest.mark.parametrize("env", sorted(DATA["profiles"]))
def test_profile_describes_full_collected_set(env: str) -> None:
    profile = DATA["profiles"][env]
    collected = DATA["canon"]["collected"]
    assert profile["passed"] + profile["admission_skips"] == collected, (
        f"profiles[{env}]: {profile['passed']} + {profile['admission_skips']} "
        f"!= canon.collected {collected} (env-skip вычитается проверкой, а не в профиле)"
    )
