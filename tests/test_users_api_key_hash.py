"""SEC: users.api_key_hash — пользователь ищется по хэшу ключа, а не по сырому ключу.

Контракт PR-F2b-1 (шаг 1 миграции ключей пользователей):

  * ``migrations/017`` добавляет ``users.api_key_hash``, заполняет его sha256 от
    существующего ``api_key`` и ставит partial UNIQUE — без обнуления plaintext;
  * паритет sqlite (``db.init_db``) делает то же самое и идемпотентен;
  * все писатели (``create_user_with_password`` / ``create_email_user`` /
    ``upsert_oauth_user``) пишут ``api_key_hash`` вместе с ``api_key``;
  * ``find_user_by_api_key`` / ``is_email_verified`` ищут по хэшу;
  * сессия браузера не хранит ключ вовсе;
  * source-pin: в db-слое нет поиска пользователя по сырому ``api_key``.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
from auth.sessions import _sessions, create_session, delete_session

REPO_ROOT = Path(__file__).resolve().parent.parent
HEX64 = re.compile(r"^[0-9a-f]{64}$")
RAW_LOOKUP = re.compile(r"WHERE\s+api_key\s*=")


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _row(user_id: str) -> dict:
    c = db._conn()
    try:
        row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    finally:
        c.close()
    assert row is not None, f"users/{user_id} не найден"
    return dict(row)


def _delete_users(user_ids: list[str]) -> None:
    if not user_ids:
        return
    c = db._conn()
    try:
        c.executemany("DELETE FROM users WHERE id = ?", [(uid,) for uid in user_ids])
        c.commit()
    finally:
        c.close()


@pytest.fixture
def user_factory(sqlite_tmp_db):
    """Пользователь через адаптер (sqlite-ветка) + уборка строки после теста."""
    adapter = sqlite_tmp_db
    db.init_db()  # схема в редиректнутой sqlite-БД создаётся только здесь
    created: list[str] = []

    def _create() -> tuple[str, str]:
        user_id = _uniq("user")
        tenant_id = _uniq("tenant")
        raw = _uniq("roma-key")
        adapter.seed_tenants({raw: {"tenant_id": tenant_id, "name": "users-key-hash"}})
        adapter.upsert_oauth_user(user_id, f"{user_id}@example.com", "Test", "google", tenant_id, raw)
        created.append(user_id)
        return user_id, raw

    yield _create

    _delete_users(created)


def test_upsert_oauth_user_writes_hash(user_factory):
    """(а) upsert_oauth_user кладёт в users.api_key_hash sha256 ключа."""
    user_id, raw = user_factory()

    row = _row(user_id)
    assert row["api_key_hash"] == _digest(raw)
    assert HEX64.match(row["api_key_hash"]), "api_key_hash обязан быть sha256-hex"
    assert row["api_key"] == raw, "шаг 1 plaintext не обнуляет"


@pytest.mark.pg
def test_create_user_with_password_writes_hash():
    """(а) create_user_with_password (PG-only) тоже пишет api_key_hash."""
    import db_adapter

    user_id = _uniq("user")
    email = f"{user_id}@example.com"
    raw = _uniq("roma-key")

    db_adapter.create_user_with_password(
        user_id,
        email,
        "Test",
        _uniq("tenant"),
        raw,
        "pbkdf2-stub",
        "token-stub",
        "2030-01-01T00:00:00Z",
    )

    row = db_adapter.get_user_by_email(email)
    assert row is not None
    assert row["api_key_hash"] == _digest(raw)
    assert HEX64.match(row["api_key_hash"])


def test_find_user_by_api_key_uses_hash_only(user_factory):
    """(б) поиск идёт по хэшу: строка находится, даже когда plaintext в ней стёрт."""
    import db_adapter

    user_id, raw = user_factory()

    c = db._conn()
    try:
        c.execute("UPDATE users SET api_key = '' WHERE id = ?", (user_id,))
        c.commit()
    finally:
        c.close()

    found = db_adapter.find_user_by_api_key(raw)
    assert found is not None and found["id"] == user_id, "поиск по хэшу должен найти пользователя"
    assert db_adapter.find_user_by_api_key(_uniq("roma-other")) is None
    assert db_adapter.find_user_by_api_key(_digest(raw)) is None, "дайджест — не ключ"


@pytest.mark.pg
def test_is_email_verified_uses_hash_lookup():
    """(б) is_email_verified находит пользователя по хэшу (PG-ветка, есть email_verified)."""
    import db_adapter
    import psycopg2

    from auth.verification import is_email_verified

    user_id = _uniq("user")
    email = f"{user_id}@example.com"
    raw = _uniq("roma-key")
    db_adapter.create_user_with_password(
        user_id,
        email,
        "Test",
        _uniq("tenant"),
        raw,
        "pbkdf2-stub",
        "token-stub",
        "2030-01-01T00:00:00Z",
    )

    assert is_email_verified(raw) is False, "пользователь найден, email не подтверждён"

    conn = psycopg2.connect(os.environ["PG_DSN"])
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET email_verified = true WHERE id = %s", (user_id,))
        conn.commit()
    finally:
        conn.close()

    assert is_email_verified(raw) is True
    assert is_email_verified(_uniq("roma-other")) is False


def test_session_does_not_store_api_key():
    """(в) в сессии нет api_key — только tenant_id и таймстемпы."""
    session_id = create_session(_uniq("tenant"))
    try:
        session = _sessions[session_id]
        assert "api_key" not in session
        assert set(session) == {"tenant_id", "created_at", "expires_at"}
    finally:
        delete_session(session_id)


def test_no_raw_key_lookup_in_db_layer():
    """(г) source-pin: в db-слое нет SELECT по сырому users.api_key."""
    offenders: list[str] = []
    for path in (REPO_ROOT / "db.py", REPO_ROOT / "db_pg.py", REPO_ROOT / "db_pg_sync.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if RAW_LOOKUP.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")

    assert offenders == [], "поиск по сырому api_key запрещён:\n" + "\n".join(offenders)


def test_sqlite_init_db_is_idempotent():
    """(д) sqlite-паритет 017: повторный init_db не падает и колонка на месте."""
    db.init_db()
    db.init_db()

    c = db._conn()
    try:
        cols = {row[1] for row in c.execute("PRAGMA table_info(users)").fetchall()}
    finally:
        c.close()
    assert "api_key_hash" in cols


def test_sqlite_backfills_existing_rows():
    """(д) sqlite-бэкфилл: строка с plaintext и пустым хэшем получает хэш при init_db."""
    db.init_db()
    user_id = _uniq("user")
    tenant_id = _uniq("tenant")
    raw = _uniq("roma-key")
    db.seed_tenants({raw: {"tenant_id": tenant_id, "name": "legacy-backfill"}})

    c = db._conn()
    try:
        c.execute(
            "INSERT INTO users (id, email, name, provider, tenant_id, api_key, api_key_hash) VALUES (?,?,?,?,?,?,?)",
            (user_id, f"{user_id}@example.com", "Legacy", "email", tenant_id, raw, ""),
        )
        c.commit()
    finally:
        c.close()

    try:
        db.init_db()
        row = _row(user_id)
        assert row["api_key_hash"] == _digest(raw)
        assert row["api_key"] == raw
    finally:
        _delete_users([user_id])


@pytest.mark.pg
def test_migration_017_is_idempotent():
    """(д) 017 применяется дважды подряд в одной транзакции без ошибок."""
    import psycopg2

    sql = (REPO_ROOT / "migrations" / "017_users_api_key_hash.sql").read_text(encoding="utf-8")

    conn = psycopg2.connect(os.environ["PG_DSN"])
    try:
        cur = conn.cursor()
        cur.execute(sql)
        cur.execute(sql)

        cur.execute("SELECT count(*), count(nullif(api_key_hash, '')) FROM users")
        total, hashed = cur.fetchone()
        assert total == hashed, f"бэкфилл неполный: {hashed} из {total}"
    finally:
        conn.rollback()
        conn.close()


def test_conflicting_upsert_keeps_old_hash(user_factory):
    """Повторный upsert того же id с другим ключом сохраняет старый api_key_hash."""
    import db_adapter

    user_id, raw = user_factory()
    before = _row(user_id)

    db.upsert_oauth_user(user_id, "other@example.com", "Test", "google", before["tenant_id"], "roma-other-key")

    after = _row(user_id)
    assert after["api_key_hash"] == before["api_key_hash"] == _digest(raw)
    assert after["api_key"] == raw, "plaintext тоже не должен меняться"
    assert db_adapter.find_user_by_api_key(raw)["id"] == user_id


def test_key_digest_rejects_empty_key():
    """sha256("") не должен приниматься за ключ ни в одной ветке."""
    import deps

    with pytest.raises(ValueError):
        deps.key_digest("")
    with pytest.raises(ValueError):
        deps.key_digest(None)


def test_lookup_with_empty_key_returns_none(user_factory):
    """Пустой ключ не должен находить пользователя с хэшем пустой строки."""
    import db_adapter

    user_factory()
    assert db.get_user_by_api_key("") is None
    assert db_adapter.find_user_by_api_key("") is None


@pytest.mark.pg
def test_pg_get_user_by_api_key_empty_returns_none():
    """PG-ветка: пустой ключ отсекается до хэширования и до пула."""
    import asyncio

    import db_pg

    assert asyncio.run(db_pg.get_user_by_api_key("")) is None
