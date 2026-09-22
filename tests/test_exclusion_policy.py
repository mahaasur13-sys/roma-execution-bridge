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
    addopts = (
        _pyproject()
        .get("tool", {})
        .get("pytest", {})
        .get("ini_options", {})
        .get("addopts", "")
    )
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
    assert (
        not missing
    ), "исключения без тройки reason·issue·expiry (слепой ignore):\n  " + "\n  ".join(
        missing
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


# ---------------------------------------------------------------------------
# A-3 (N7c/N7d): политика исключений обязана покрывать не только --ignore,
# но и xfail. Иначе зелёный CI скрывает сломанную функциональность ровно так же,
# как её скрывал безымянный --ignore.
# ---------------------------------------------------------------------------

XFAIL_RE = re.compile(r"@pytest\.mark\.xfail\((?P<body>.*?)\n\s{0,8}\)", re.DOTALL)
XFAIL_FILES = sorted(
    [p for p in REPO_ROOT.rglob("test_*.py") if ".venv" not in p.parts]
)


def audit_xfail_blocks(text: str) -> list[str]:
    """Проверяет ОДИН блок xfail. Вынесено отдельно, чтобы негатив мог её вызвать."""
    problems = []
    if "strict=True" not in text:
        problems.append(
            "xfail без strict=True: при починке молча станет XPASS и дефект потеряется"
        )
    if not re.search(r"issue:\s*\S+", text):
        problems.append("xfail без issue-id")
    m = re.search(r"expiry:\s*(\d{4}-\d{2}-\d{2})", text)
    if not m:
        problems.append("xfail без expiry (YYYY-MM-DD)")
    else:
        try:
            if dt.date.fromisoformat(m.group(1)) <= dt.date.today():
                problems.append(
                    f"xfail просрочен (expiry={m.group(1)}) — починить или продлить осознанно"
                )
        except ValueError:
            problems.append(f"xfail expiry не дата: {m.group(1)!r}")
    return problems


def test_every_xfail_has_issue_expiry_and_strict() -> None:
    offenders = []
    for path in XFAIL_FILES:
        for block in XFAIL_RE.finditer(path.read_text(encoding="utf-8")):
            for problem in audit_xfail_blocks(block.group("body")):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {problem}")
    assert (
        not offenders
    ), "xfail без тройки reason·issue·expiry или без strict:\n  " + "\n  ".join(
        offenders
    )


def test_xfail_policy_can_actually_fail() -> None:
    """НЕГАТИВ (доктрина: у каждого детектора есть негативный тест, иначе его не существует)."""
    bad = 'reason=("R-5: что-то сломано", ),'
    problems = audit_xfail_blocks(bad)
    assert (
        len(problems) >= 3
    ), f"детектор xfail не сработал на заведомо плохом блоке: {problems}"
    good = 'strict=True, reason=("issue: P1-A · expiry: 2099-01-01 · причина",)'
    assert (
        audit_xfail_blocks(good) == []
    ), "детектор xfail ложно краснеет на корректном блоке"


# ---------------------------------------------------------------------------
# A-3b: политика исключений обязана покрывать skip-механизмы.
# Дефект класса, а не инстанс: детектор знал только --ignore и xfail. В дереве
# 16 мест skip/skipif, и ни одно не несло полной тройки; 6 из них исполнялись
# в каноническом прогоне (JUnit: skipped=8) — то есть «зелёный прогон» молча нёс
# незакрытые проверки. Правило то же, что для xfail:
#     reason · issue: <ID> · expiry: YYYY-MM-DD
# ---------------------------------------------------------------------------

# Экранирование в шаблонах — не косметика: этот файл сканируется наравне с прочими,
# и неэкранированный образец делал бы его нарушителем собственного правила.
SKIP_PATTERNS = (
    (re.compile(r"pytest\.skip\("), "skip"),
    (re.compile(r"pytest\.mark\.skipif"), "skipif-маркер"),
    (re.compile(r"pytest\.mark\.skip\b"), "skip-маркер"),
    (re.compile(r"pytest\.importorskip\("), "importorskip"),
    (re.compile(r"pytest\.xfail\("), "императивный xfail"),
)
DECORATOR_KINDS = {"skipif-маркер", "skip-маркер"}
MAX_CALL_LINES = 6
SKIP_GLOBS = ("test_*.py", "*_test.py", "conftest.py")

# ---------------------------------------------------------------------------
# G-CI-PG-CANON (шестой механизм сканера): программный env-skip из conftest.
# Класс дефекта: скип, вставленный хуком коллекции, НЕ виден текстовым паттернам
# `pytest.skip` в тест-файлах — а именно он определяет, исполнились ли
# PG-зависимые проверки. Поэтому контракт проверяется на месте объявления:
#     ENV_SKIP_MARKER (класс различается машинно) · issue: <ID> · expiry: YYYY-MM-DD
# и на месте инъекции: причина обязана браться из единственного объявления, иначе
# объявление и фактическая причина расходятся (скип без тройки, но «по правилам»).
# ---------------------------------------------------------------------------
CONFTEST = REPO_ROOT / "tests" / "conftest.py"
COMPLETENESS_CHECKER = REPO_ROOT / "scripts" / "ci_run_completeness.py"
ENV_SKIP_KIND = "env-skip-инъекция"
ENV_SKIP_MARKER_RE = re.compile(r"ENV_SKIP_MARKER\s*=\s*[\"'](?P<marker>[^\"']+)[\"']")
ENV_SKIP_REASON_RE = re.compile(
    r"^ENV_SKIP_REASON\s*=\s*\((?P<body>.*?)^\)", re.DOTALL | re.MULTILINE
)
ENV_SKIP_INJECT_RE = re.compile(r"reason=ENV_SKIP_REASON\b")


def _call_window(lines: list[str], index: int) -> str:
    """Логический вызов: от строки места до закрывающей скобки (не длиннее MAX_CALL_LINES).

    Окно именно вызова, а не «±4 строки»: иначе тройка соседнего теста маскировала бы
    голый skip рядом — ровно тот класс слепоты, против которого это правило и написано.
    """
    window = [lines[index - 1]]
    depth = window[0].count("(") - window[0].count(")")
    cursor = index
    while depth > 0 and cursor < len(lines) and len(window) < MAX_CALL_LINES:
        nxt = lines[cursor]
        window.append(nxt)
        depth += nxt.count("(") - nxt.count(")")
        cursor += 1
    return "\n".join(window)


def audit_env_skip_declaration(text: str) -> list[str]:
    """Шестой механизм: контракт объявления env-skip. Вынесено отдельно — негатив вызывает её."""
    problems = []
    marker = ENV_SKIP_MARKER_RE.search(text)
    if not marker:
        problems.append(
            "нет объявления ENV_SKIP_MARKER (класс env-скипа неразличим машинно)"
        )
    elif not marker.group("marker").startswith("env-skip"):
        problems.append(
            f"маркер env-скипа не начинается с 'env-skip': {marker.group('marker')!r}"
        )
    if not ENV_SKIP_INJECT_RE.search(text):
        problems.append(
            "причина env-скипа не берётся из ENV_SKIP_REASON — объявление и факт расходятся"
        )
    reason = ENV_SKIP_REASON_RE.search(text)
    if not reason:
        problems.append("нет объявления ENV_SKIP_REASON")
    body = reason.group("body") if reason else ""
    if not re.search(r"issue:\s*\S+", body):
        problems.append("env-skip без issue-id")
    m = re.search(r"expiry:\s*(\d{4}-\d{2}-\d{2})", body)
    if not m:
        problems.append("env-skip без expiry (YYYY-MM-DD)")
    else:
        try:
            if dt.date.fromisoformat(m.group(1)) <= dt.date.today():
                problems.append(
                    f"env-skip просрочен (expiry={m.group(1)}) — починить или продлить осознанно"
                )
        except ValueError:
            problems.append(f"env-skip expiry не дата: {m.group(1)!r}")
    return problems


def audit_skip_block(text: str, kind: str = "skip") -> list[str]:
    """Проверяет ОДНО место skip-механизма. Вынесено отдельно, чтобы негатив мог её вызвать."""
    if kind == ENV_SKIP_KIND:
        return audit_env_skip_declaration(text)
    problems = []
    if kind in DECORATOR_KINDS:
        if not re.search(r"reason\s*=", text):
            problems.append("skip-маркер без reason=")
    elif "reason=" not in text and not re.search(r"\(\s*[\"']", text):
        problems.append("skip без причины (нет строкового аргумента и нет reason=)")
    if not re.search(r"issue:\s*\S+", text):
        problems.append("skip без issue-id")
    m = re.search(r"expiry:\s*(\d{4}-\d{2}-\d{2})", text)
    if not m:
        problems.append("skip без expiry (YYYY-MM-DD)")
    else:
        try:
            if dt.date.fromisoformat(m.group(1)) <= dt.date.today():
                problems.append(
                    f"skip просрочен (expiry={m.group(1)}) — починить или продлить осознанно"
                )
        except ValueError:
            problems.append(f"skip expiry не дата: {m.group(1)!r}")
    return problems


def find_skip_sites(text: str) -> list[tuple[int, str, str]]:
    """Места skip-механизмов в ОДНОМ тексте: (строка, вид, окно вызова)."""
    lines = text.splitlines()
    sites = []
    for index, line in enumerate(lines, start=1):
        for pattern, kind in SKIP_PATTERNS:
            if pattern.search(line):
                window = _call_window(lines, index)
                if ENV_SKIP_INJECT_RE.search(window):
                    # Шестой механизм: причина берётся из объявления в conftest,
                    # поэтому аудиту предъявляется файл целиком, а не окно вызова.
                    sites.append((index, ENV_SKIP_KIND, text))
                else:
                    sites.append((index, kind, window))
    return sites


def skip_files() -> list[Path]:
    """Все тест-файлы дерева (включая вложенные наборы и сам файл политики)."""
    found = {p for glob in SKIP_GLOBS for p in REPO_ROOT.rglob(glob)}
    return sorted(
        p for p in found if ".venv" not in p.parts and "__pycache__" not in p.parts
    )


def test_every_skip_has_issue_and_expiry() -> None:
    offenders = []
    for path in skip_files():
        for line, kind, window in find_skip_sites(path.read_text(encoding="utf-8")):
            for problem in audit_skip_block(window, kind):
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{line} ({kind}): {problem}"
                )
    assert not offenders, (
        "места skip-механизмов без тройки reason·issue·expiry (невидимое исключение):\n  "
        + "\n  ".join(offenders)
    )


def test_skip_policy_can_actually_fail() -> None:
    """НЕГАТИВ (доктрина: детектор без негативного теста не существует).

    Образцы собираются конкатенацией: файл политики сканируется тем же шаблоном,
    поэтому литеральный образец сделал бы его нарушителем собственного правила.
    """
    bare = "pytest." + 'skip("PG недоступен")'
    problems = audit_skip_block(bare, "skip")
    assert (
        len(problems) >= 2
    ), f"детектор skip не сработал на заведомо плохом блоке: {problems}"

    good = (
        "pytest."
        + 'skip("PG недоступен — только живой PG; issue: P1-C · expiry: 2099-01-01")'
    )
    assert (
        audit_skip_block(good, "skip") == []
    ), "детектор skip ложно краснеет на корректном блоке"

    bad_marker = "@pytest.mark." + "skipif(True)"
    assert audit_skip_block(
        bad_marker, "skipif-маркер"
    ), "skipif без тройки обязан падать"

    good_marker = "@pytest.mark." + (
        'skipif(True, reason="ждём живой PG · issue: P1-C · expiry: 2099-01-01")'
    )
    assert (
        audit_skip_block(good_marker, "skipif-маркер") == []
    ), "корректный skipif ложно краснеет"


def test_skip_scanner_covers_synthetic_file_and_itself() -> None:
    """Структурный негатив: сканер видит голый skip в синтетическом тексте и не исключает себя."""
    synthetic = "def test_x():\n    pytest." + 'skip("просто так")\n'
    sites = find_skip_sites(synthetic)
    assert sites, "сканер не увидел голый skip в синтетическом тексте"
    assert any(
        audit_skip_block(window, kind) for _, kind, window in sites
    ), "голый skip в синтетическом тексте не распознан как нарушение"
    assert any(
        p.name == "test_exclusion_policy.py" for p in skip_files()
    ), "файл политики исключён из собственного скана — дыра в правиле"


def test_runtime_skip_budget_requires_issue_and_expiry() -> None:
    """Рантайм-бюджет скипов (conftest) обязан требовать ту же тройку, что и статическая политика.

    Два разных порога на одно явление — источник дефекта (класс, найденный в A-6):
    статический скан требовал тройку, рантайм-бюджет — только issue, из-за чего
    «зелёный» прогон был слабее, чем читался.
    """
    conftest = (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "EXPIRY_MARK" in conftest, "рантайм-бюджет скипов не знает про expiry"
    budget = conftest.split("def pytest_sessionfinish", 1)[-1]
    assert (
        "ISSUE_MARK" in budget and "EXPIRY_MARK" in budget
    ), "рантайм-бюджет скипов проверяет не всю тройку issue+expiry"


# ---------------------------------------------------------------------------
# G-CI-PG-CANON: контракт шестого механизма — на самом файле, где он объявлен.
# ---------------------------------------------------------------------------


def test_env_skip_declaration_is_complete() -> None:
    """Реальный conftest: класс env-скипа различим, тройка полная и не просрочена."""
    problems = audit_env_skip_declaration(CONFTEST.read_text(encoding="utf-8"))
    assert (
        not problems
    ), "контракт env-skip нарушен в tests/conftest.py:\n  " + "\n  ".join(problems)


def test_env_skip_marker_is_single_source() -> None:
    """Класс скипа различается МАШИННО: маркер обязан совпадать у conftest и чекера канона."""
    decl = ENV_SKIP_MARKER_RE.search(CONFTEST.read_text(encoding="utf-8"))
    assert decl, "в tests/conftest.py нет объявления ENV_SKIP_MARKER"
    marker = decl.group("marker")
    checker = COMPLETENESS_CHECKER.read_text(encoding="utf-8")
    assert marker in checker, (
        f"маркер env-скипа {marker!r} не используется чекером канона "
        f"({COMPLETENESS_CHECKER.relative_to(REPO_ROOT)}) — класс скипа не проверяется"
    )


def test_env_skip_contract_can_actually_fail() -> None:
    """НЕГАТИВ: без тройки и без единого объявления детектор обязан краснеть."""
    bad = (
        'ENV_SKIP_MARKER = "pg-skip"\n'
        "ENV_SKIP_REASON = (\n"
        '    "нет PG"\n'
        ")\n"
        "skip = pytest.mark." + 'skip(reason="нет PG")\n'
    )
    problems = audit_env_skip_declaration(bad)
    assert len(problems) >= 3, f"детектор env-skip не сработал: {problems}"
    good = (
        'ENV_SKIP_MARKER = "env-skip:pg-unavailable"\n'
        "ENV_SKIP_REASON = (\n"
        '    "env-skip:pg-unavailable · issue: G-CI-PG-CANON · expiry: 2099-01-01"\n'
        ")\n"
        "skip = pytest.mark." + "skip(reason=ENV_SKIP_REASON)\n"
    )
    assert (
        audit_env_skip_declaration(good) == []
    ), "детектор env-skip ложно краснеет на корректном объявлении"


def test_env_skip_injection_is_scanned_as_its_own_mechanism() -> None:
    """Структурный негатив: инъекция из conftest распознаётся шестым механизмом, а не «голым skip»."""
    synthetic = (
        "def test_x():\n"
        "    skip = pytest.mark." + "skip(reason=ENV_SKIP_REASON)\n"
        "    item.add_marker(skip)\n"
    )
    kinds = {kind for _, kind, _ in find_skip_sites(synthetic)}
    assert (
        ENV_SKIP_KIND in kinds
    ), f"инъекция env-skip не распознана отдельным механизмом: {kinds or 'пусто'}"
