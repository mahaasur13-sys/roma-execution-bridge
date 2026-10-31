"""E2 guard: named admission не может нести маркер pg.

Иначе без PG_DSN conftest (pytest_collection_modifyitems) переводит его в
env-skip:pg-unavailable, и checker считает один тест дважды: env-skip и
отсутствующий admission. Ожидания берутся из канона (.ci/run-completeness.json),
отдельно для каждого профиля.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CANON = json.loads((REPO_ROOT / ".ci" / "run-completeness.json").read_text(encoding="utf-8"))
PG_MARKER = "pg"


def _marks(obj) -> list:
    marks = getattr(obj, "pytestmark", [])
    return marks if isinstance(marks, list) else [marks]


@pytest.mark.parametrize("profile", sorted(CANON["profiles"]))
def test_named_admissions_are_not_pg_marked(profile):
    offenders = []
    for entry in CANON["profiles"][profile]["named_admissions"]:
        path, _, func_name = entry["test"].partition("::")
        module = importlib.import_module(path[: -len(".py")].replace("/", "."))
        func = getattr(module, func_name.split("[")[0])
        names = {m.name for m in _marks(func) + _marks(module)}
        if PG_MARKER in names:
            offenders.append(entry["test"])
    assert offenders == [], f"profile {profile}: named admission с маркером pg → env-skip без PG_DSN: {offenders}"
