"""E-01: политика исключений pytest — причина · issue · срок пересмотра.

Зачем: в pyproject.toml накопились три безымянных `--ignore` (test_ci.py,
saas/gateway/tests/test_gateway.py, saas/gateway/tests/test_rate_limiter.py) —
без причины, без issue-id и без срока. Это тот же класс, что «skip без reason»,
только на уровне КОЛЛЕКЦИИ: файл не просто исключён, а исключён НЕВИДИМО.

Правило: исключение допустимо только с тройкой:
    reason  — что именно не работает и почему это осознано
    issue   — идентификатор для трассировки
    expiry  — дата пересмотра (YYYY-MM-DD); после неё suite краснеет

Тройка живёт в [tool.pytest_exclusions] в pyproject.toml, потому что рядом
с самим исключением (в addopts) её негде хранить. Без записи — нет исключения.
"""

import datetime as dt
import re
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.ops

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"

IGNORE_RE = re.compile(r"--ignore=([^\s\"']+)")


def _pyproject() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def _declared_ignores() -> list[str]:
    addopts = _pyproject().get("tool", {}).get("pytest", {}).get("ini_options", {}).get("addopts", "")
    return IGNORE_RE.findall(addopts)


def test_every_ignore_has_reason_issue_expiry() -> None:
    audit = _pyproject().get("tool", {}).get("pytest_exclusions", {})
    missing = []
    for path in _declared_ignores():
        entry = audit.get(path)
        if not entry:
            missing.append(f"{path}: нет записи в [tool.pytest_exclusions]")
            continue
        for field in ("reason", "issue", "expiry"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                missing.append(f"{path}: пустое/отсутствующее поле {field}")
    assert not missing, (
        "исключения без тройки reason·issue·expiry (слепой ignore):\n  " + "\n  ".join(missing)
    )


def test_no_orphan_audit_entries() -> None:
    """Запись без соответствующего --ignore — тоже расхождение: правило описывает исключение, которого нет."""
    audit = _pyproject().get("tool", {}).get("pytest_exclusions", {})
    declared = set(_declared_ignores())
    orphans = sorted(set(audit) - declared)
    assert not orphans, f"записи в exclusion_audit без --ignore: {orphans}"


def test_ignored_paths_exist() -> None:
    """Стейл-ignore («исключили и забыли, файл переименован/удалён») — это ложь в конфиге."""
    absent = [p for p in _declared_ignores() if not (REPO_ROOT / p).exists()]
    assert not absent, f"--ignore указывает на несуществующие пути: {absent}"


def test_exclusions_are_not_expired() -> None:
    """Истёкшая тройка = пора решать: починить или продлить осознанно (но не молча)."""
    audit = _pyproject().get("tool", {}).get("pytest_exclusions", {})
    today = dt.date.today()
    expired = []
    for path, entry in audit.items():
        raw = entry.get("expiry", "")
        try:
            expiry = dt.date.fromisoformat(raw)
        except ValueError:
            expired.append(f"{path}: expiry={raw!r} не дата формата YYYY-MM-DD")
            continue
        if expiry <= today:
            expired.append(
                f"{path}: исключение просрочено (expiry={expiry.isoformat()}, "
                f"issue={entry.get('issue')}) — починить или продлить осознанно"
            )
    assert not expired, "\n  ".join(["просроченные исключения:"] + expired)
