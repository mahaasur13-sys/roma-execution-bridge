"""A1: изоляция тестов от боевого кластера PostgreSQL.

Проблема (грабли Г5/Г6), которую закрывает этот файл:
  `env_loader.load_env()` подтягивал из `.env` боевой `PG_DSN`, поэтому
  инвариантные тесты исполнялись против боевой БД и писали строки в
  append-only `ledger_entries` (удалить их нельзя — триггер immutability).

Контракт:
  1. Тесты работают только с тестовой БД `roma_test` того же кластера
     (DSN выводится из боевого заменой имени БД) — без ручной настройки.
  2. Пустое значение DSN не пинится: переменная снимается (`pop`), чтобы
     `env_loader` мог инжектить значение из `.env`. Дефект `""→None`:
     `os.environ.get("PG_DSN", "").strip()` возвращает `""`, а не `None`,
     поэтому ветка вывода тестового DSN не срабатывала, а в окружение
     попадала пустая строка — PG-инварианты молча скипались.
  2a. Явно заданный в окружении `PG_DSN` на НЕ тестовую БД → REFUSED (exit 90).
     Молчаливая подмена значения оператора на `roma_test` запрещена: намеренный
     прогон против боевой БД должен падать громко, а не «зеленеть» на другой БД.
     Вывод тестового DSN выполняется только когда `PG_DSN` пуст/не задан.
  3. Сравнение имени БД — ТОЧНОЕ (не подстрока): разрешён только `roma_test`;
     `roma`, `roma2`, `roma_prod`, `roma_test_backup` и любое другое имя → REFUSED.
  4. Поддержаны обе формы DSN: URL (`postgresql://…`, в т.ч. `+driver`) и
     libpq (`key=value`). Пароль с `/` или `:` разбор не ломает.
  5. Sweep db-ключей — `PG_DSN · DB_DSN · DATABASE_URL · POSTGRES_DSN ·
     ROMA_DSN · SQLALCHEMY_DATABASE_URI` и libpq-переменные `PGHOST · PGPORT ·
     PGDATABASE · PGUSER · PGPASSWORD`: после `resolve()` ни один не указывает
     на боевую БД. В лог попадают только host и dbname (без креденшлов).
  6. Отказ — `IsolationRefused` → exit-код 90 + маркер `A1-REFUSED` в stdout
     (не 1/2/4: в CI отказ изоляции не должен выглядеть как «tests failed»
     или «pytest usage error»).
  7. Advisory-локи изолированы: они живут в пространстве БД, а `roma_test` —
     отдельная БД кластера.
  8. Тестовые tenant-id несут префикс `test-` (см. `tenant()`).

Переменные окружения:
  ROMA_TEST_PG_DSN / TEST_PG_DSN — явный тестовый DSN (приоритет выше всего)
  ROMA_PROD_PG_DSN               — явное указание боевого DSN для сравнения
"""

from __future__ import annotations

import os
import re
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROD_DB_NAME = "roma"
TEST_DB_NAME = "roma_test"
ALLOWED_DB_NAMES = frozenset({TEST_DB_NAME})
TEST_TENANT_PREFIX = "test-"
EXPLICIT_DSN_VARS = ("ROMA_TEST_PG_DSN", "TEST_PG_DSN")
DSN_VARS = (
    "PG_DSN",
    "DB_DSN",
    "DATABASE_URL",
    "POSTGRES_DSN",
    "ROMA_DSN",
    "SQLALCHEMY_DATABASE_URI",
)
LIBPQ_VARS = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "", "[::1]"}
_URL_DRIVER_RE = re.compile(r"^([a-z0-9]+)\+[a-z0-9_]+://", re.IGNORECASE)
_LIBPQ_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*('(?:[^']|'')*'|[^\s']*)")

REFUSED_EXIT_CODE = 90
REFUSED_MARKER = "A1-REFUSED"


class IsolationRefused(RuntimeError):
    """Изоляция тестов не обеспечена — прогон запрещён (fail-closed)."""


ProductionDsnRefused = IsolationRefused


def _env_or_none(var: str) -> str | None:
    """Значение env как есть или None: пустая строка приравнена к отсутствию."""
    raw = os.environ.get(var)
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value or None


def _env_file_dsn() -> str | None:
    for candidate in (Path.cwd() / ".env", REPO_ROOT / ".env"):
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if line.startswith("PG_DSN="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    return value
    return None


def parse_dsn(raw: str | None) -> dict | None:
    """Разобрать DSN в URL- или libpq-форме → {form, host, port, dbname, scheme}."""
    dsn = (raw or "").strip()
    if not dsn:
        return None
    normalized = _URL_DRIVER_RE.sub(r"\1://", dsn)
    if "://" in normalized:
        parsed = urllib.parse.urlparse(normalized)
        scheme = (parsed.scheme or "").lower()
        path = (parsed.path or "").lstrip("/")
        dbname = path.split("/")[-1] if path else ""
        return {
            "form": "url",
            "scheme": scheme,
            "host": parsed.hostname,
            "port": parsed.port,
            "dbname": urllib.parse.unquote(dbname),
        }
    values: dict[str, str] = {}
    for match in _LIBPQ_KV_RE.finditer(dsn):
        key, value = match.group(1).lower(), match.group(2)
        if len(value) >= 2 and value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("''", "'")
        values[key] = value
    port = values.get("port", "")
    return {
        "form": "libpq",
        "scheme": "postgresql",
        "host": values.get("host"),
        "port": int(port) if port.isdigit() else None,
        "dbname": values.get("dbname", ""),
    }


def identity(dsn: str | None) -> tuple[str, int, str]:
    """(host, port, dbname) — без креденшлов."""
    parsed = parse_dsn(dsn) or {}
    host = (parsed.get("host") or "").lower()
    if host in _LOCAL_HOSTS:
        host = "localhost"
    return (host, parsed.get("port") or 5432, parsed.get("dbname") or "")


def describe(dsn: str | None) -> str:
    """Маскированное описание DSN (только host/port/dbname)."""
    host, port, dbname = identity(dsn)
    return f"host={host} port={port} db={dbname}"


def derive_dsn(dsn: str, dbname: str) -> str | None:
    """Хирургически заменить имя БД, сохранив форму DSN и креденшлы."""
    parsed = parse_dsn(dsn)
    if not parsed:
        return None
    if parsed["form"] == "url":
        normalized = _URL_DRIVER_RE.sub(r"\1://", dsn.strip())
        url = urllib.parse.urlparse(normalized)
        return urllib.parse.urlunparse(url._replace(path="/" + dbname))
    text = dsn.strip()
    if re.search(r"(?:^|\s)dbname\s*=", text, re.IGNORECASE):
        return re.sub(
            r"((?:^|\s)dbname\s*=\s*)('(?:[^']|'')*'|\S+)",
            lambda m: m.group(1) + dbname,
            text,
            count=1,
            flags=re.IGNORECASE,
        )
    return f"{text} dbname={dbname}"


def derive_test_dsn(dsn: str) -> str | None:
    """DSN той же БД-формы, но с тестовым именем БД (`roma_test`)."""
    return derive_dsn(dsn, TEST_DB_NAME)


def prod_dsn() -> str | None:
    """Боевой DSN: явная переменная или значение из `.env`."""
    return _env_or_none("ROMA_PROD_PG_DSN") or _env_file_dsn()


def test_db_exists(dsn: str) -> bool:
    """Доступна ли тестовая БД (fail-closed при недоступности)."""
    try:
        import psycopg2

        with psycopg2.connect(dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception:
        return False


def _refusal(dsn: str | None, reason: str) -> IsolationRefused:
    allowed = ", ".join(sorted(ALLOWED_DB_NAMES))
    return IsolationRefused(
        f"{REFUSED_MARKER}: тестовый DSN не изолирован от боевой БД "
        f"({describe(dsn)}; {reason}).\n"
        "Тесты не запускаются против боевого кластера: их вставки в append-only\n"
        f"ledger_entries необратимы. Разрешена только тестовая БД '{allowed}' — "
        "сравнение имени точное.\n"
        f"  ROMA_TEST_PG_DSN=postgresql://<user>:<pass>@<host>:<port>/{TEST_DB_NAME} pytest tests/\n"
        "или ничего не задавайте — тестовый DSN будет выведен из боевого заменой имени БД.\n"
        "Создание тестовой БД (идемпотентно): python scripts/ensure_test_db.py"
    )


def sweep_db_keys(*, log: bool = False) -> tuple[dict[str, str | None], list[str]]:
    """Проверить все db-подобные ключи: ни один не должен указывать на боевую БД.

    Возвращает (карта «ключ → host/dbname или None», список нарушений). Значения
    креденшлов не возвращаются и не логируются — только host и dbname.
    """
    findings: dict[str, str | None] = {}
    problems: list[str] = []

    for var in DSN_VARS:
        raw = _env_or_none(var)
        if raw is None:
            findings[var] = None
            continue
        parsed = parse_dsn(raw) or {}
        scheme = (parsed.get("scheme") or "").lower()
        if "://" in raw and not scheme.startswith("postgres"):
            findings[var] = f"non-pg({scheme or 'unknown'})"
            continue
        host, _port, dbname = identity(raw)
        findings[var] = f"host={host} db={dbname}"
        if dbname and dbname not in ALLOWED_DB_NAMES:
            problems.append(f"{var} → host={host} db={dbname}")

    for var in LIBPQ_VARS:
        raw = _env_or_none(var)
        if raw is None:
            findings[var] = None
            continue
        if var == "PGDATABASE":
            findings[var] = f"db={raw}"
            if raw not in ALLOWED_DB_NAMES:
                problems.append(f"{var} → db={raw}")
        elif var in ("PGHOST", "PGPORT"):
            findings[var] = f"{var.lower()}={raw}"
        else:
            findings[var] = "set"

    if log:
        summary = " · ".join(f"{k}={v or '<unset>'}" for k, v in findings.items())
        print(f"[conftest] A1 db-key sweep (host/dbname only): {summary}")
    return findings, problems


def foreign_db_keys() -> list[str]:
    """Явно заданные db-ключи, ведущие не в разрешённую тестовую БД.

    Используется ДО `env_loader.load_env()`: оператор, экспортировавший `PG_DSN`
    боевого кластера, должен получить отказ, а не тихую подмену имени БД.
    Возвращаются только имена переменных и имя БД — без креденшлов.
    """
    problems: list[str] = []
    for var in DSN_VARS:
        raw = _env_or_none(var)
        if raw is None:
            continue
        parsed = parse_dsn(raw) or {}
        scheme = (parsed.get("scheme") or "").lower()
        if "://" in raw and not scheme.startswith("postgres"):
            continue
        dbname = identity(raw)[2]
        if dbname not in ALLOWED_DB_NAMES:
            problems.append(f"{var} -> db={dbname or '<unknown>'}")
    return problems


def sweep_summary() -> str:
    findings, problems = sweep_db_keys()
    summary = " · ".join(f"{k}={v or '<unset>'}" for k, v in findings.items())
    suffix = (
        " · violations: none"
        if not problems
        else " · violations: " + "; ".join(problems)
    )
    return summary + suffix


def explicit_pin_refusal() -> None:
    """Проверка ЯВНО заданного DSN — вызывается ДО `env_loader.load_env()`.

    Явно экспортированный `PG_DSN`/`ROMA_TEST_PG_DSN`/`TEST_PG_DSN`, который
    указывает не на `roma_test`, — это осознанное намерение оператора; молча
    переписать его на тестовую БД нельзя (иначе «PG_DSN=<боевой> pytest»
    выглядел бы как успешный тестовый прогон). Отсюда — REFUSED.
    """
    for var in ("PG_DSN",) + EXPLICIT_DSN_VARS:
        raw = _env_or_none(var)
        if raw is None:
            continue
        dbname = identity(raw)[2]
        if dbname in ALLOWED_DB_NAMES:
            os.environ[var] = raw
            continue
        raise _refusal(raw, f"явно заданный {var} указывает на dbname={dbname!r}")


def resolve(*, strict: bool = True, require_test_db: bool = True) -> str | None:
    """Разрешить и пиновать тестовый DSN (вызывать ПОСЛЕ `env_loader.load_env()`).

    Возвращает `None`, только если тестовый DSN вывести не из чего: тогда
    `PG_DSN` снимается, чтобы PG-тесты честно скипнулись (и это посчитает
    skip-budget), а не исполнились против боевой БД.
    """
    raw = os.environ.get("PG_DSN")
    dsn = raw.strip() if isinstance(raw, str) else None

    if not dsn:
        os.environ.pop("PG_DSN", None)
        source = (
            _env_or_none(EXPLICIT_DSN_VARS[0])
            or _env_or_none(EXPLICIT_DSN_VARS[1])
            or prod_dsn()
        )
        if not source:
            return None
        dsn = derive_test_dsn(source)
        if not dsn:
            return None
    else:
        # Боевой DSN, экспортированный оператором ЯВНО, отсекается в conftest ДО
        # env_loader.load_env() (foreign_db_keys, exit=90). Здесь PG_DSN может быть
        # непустым УЖЕ после загрузки .env — это штатный источник: переводим его в
        # тестовый хирургической заменой имени БД (сравнение имени точное).
        if identity(dsn)[2] not in ALLOWED_DB_NAMES:
            derived = derive_test_dsn(dsn)
            if not derived:
                return None
            dsn = derived
        os.environ["PG_DSN"] = dsn

    dbname = identity(dsn)[2]
    if dbname not in ALLOWED_DB_NAMES:
        raise _refusal(
            dsn, f"dbname={dbname!r} не входит в разрешённые {sorted(ALLOWED_DB_NAMES)}"
        )

    if require_test_db and not test_db_exists(dsn):
        raise IsolationRefused(
            f"{REFUSED_MARKER}: тестовая БД '{TEST_DB_NAME}' недоступна ({describe(dsn)}). "
            "Создать идемпотентно: python scripts/ensure_test_db.py"
        )

    os.environ["PG_DSN"] = dsn
    os.environ.setdefault("ROMA_TEST_PG_DSN", dsn)
    os.environ.setdefault("ROMA_TEST_TENANT_PREFIX", TEST_TENANT_PREFIX)

    if strict:
        _findings, problems = sweep_db_keys()
        if problems:
            raise _refusal(
                dsn, "после resolve остались ключи на боевую БД: " + "; ".join(problems)
            )

    return dsn


def resolve_or_fail() -> str | None:
    """Обёртка для conftest: бросает IsolationRefused вместо тихого skip."""
    return resolve(strict=True)


def tenant(label: str) -> str:
    """Тестовый tenant-id с обязательным префиксом `test-`."""
    prefix = os.environ.get("ROMA_TEST_TENANT_PREFIX", TEST_TENANT_PREFIX)
    return f"{prefix}{label}"
