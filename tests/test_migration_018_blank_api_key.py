"""SEC: ``migrations/018`` обнуляет plaintext ``users.api_key``.

Контракт шага 2 миграции ключей пользователей (после 017, который добавил
``api_key_hash`` и сделал его единственным путём поиска):

  * ``api_key`` объявлен NOT NULL, поэтому «строки нет» — это ``''``, а не NULL;
  * guard fail-closed: строка с plaintext без ``api_key_hash`` отменяет всю
    миграцию — иначе ключ исчез бы, не оставив замены;
  * пост-проверка в той же транзакции: 0 строк с непустым ``api_key``;
  * идемпотентность: повторный прогон не трогает ни одной строки;
  * писатели (signup / OAuth / rotate) не возвращают plaintext в столбец.
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

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION = REPO_ROOT / "migrations" / "018_blank_users_api_key.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


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


def test_migration_018_blanks_with_empty_string() -> None:
    """(1) Обнуление идёт через '', а не NULL: колонка NOT NULL."""
    sql = _sql()

    assert re.search(r"SET\s+api_key\s*=\s*''", sql), "018 обязана ставить пустую строку"
    assert not re.search(r"SET\s+api_key\s*=\s*NULL", sql, re.IGNORECASE), (
        "api_key объявлен NOT NULL — NULL упадёт на живой схеме"
    )
    assert re.search(r"WHERE\s+coalesce\(api_key,\s*''\)\s*<>\s*''", sql), (
        "WHERE пропускает уже обнулённые строки (идемпотентность)"
    )


def test_migration_018_guard_runs_before_update() -> None:
    """(2) Guard про orphans идёт до UPDATE и падает fail-closed."""
    sql = _sql()
    guard_at = sql.find("orphans")
    update_at = sql.find("UPDATE users")
    assert guard_at != -1, "в 018 нет guard-блока про пустой api_key_hash"
    assert "RAISE EXCEPTION" in sql
    assert update_at != -1
    assert guard_at < update_at, "guard обязан стоять до UPDATE"


def test_migration_018_postcheck_after_update() -> None:
    """(3) Пост-проверка «0 строк с plaintext» идёт после UPDATE."""
    sql = _sql()
    update_at = sql.find("UPDATE users")
    postcheck_at = sql.find("remaining")
    assert postcheck_at != -1, "в 018 нет пост-проверки"
    assert postcheck_at > update_at, "пост-проверка обязана идти после UPDATE"
    assert sql.count("RAISE EXCEPTION") >= 2


def test_migration_018_file_is_registered_in_order() -> None:
    """(4) Файл назван по конвенции и лежит после 017."""
    assert MIGRATION.name.startswith("018_")
    assert MIGRATION.name.endswith(".sql")
    names = sorted(p.name for p in (REPO_ROOT / "migrations").glob("*.sql"))
    assert names.index(MIGRATION.name) == names.index("017_users_api_key_hash.sql") + 1


@pytest.mark.pg
def test_migration_018_is_idempotent() -> None:
    """(5) Повторное применение в одной транзакции не падает и не оставляет plaintext."""
    import psycopg2

    sql = _sql()
    conn = psycopg2.connect(os.environ["PG_DSN"])
    try:
        cur = conn.cursor()
        cur.execute(sql)
        cur.execute(sql)

        cur.execute("SELECT count(*) FROM users WHERE coalesce(api_key, '') <> ''")
        remaining = cur.fetchone()[0]
        assert remaining == 0, f"018 оставила {remaining} строк с plaintext"
    finally:
        conn.rollback()
        conn.close()


def test_signup_and_rotate_do_not_write_plaintext(sqlite_tmp_db) -> None:
    """(6) Писатели не возвращают plaintext в users.api_key."""
    adapter = sqlite_tmp_db
    db.init_db()

    user_id = _uniq("user")
    tenant_id = _uniq("tenant")
    raw = _uniq("roma-key")
    created: list[str] = []
    try:
        adapter.seed_tenants({raw: {"tenant_id": tenant_id, "name": "migration-018"}})
        adapter.upsert_oauth_user(user_id, f"{user_id}@example.com", "Test", "google", tenant_id, raw)
        created.append(user_id)

        row = _row(user_id)
        assert row["api_key"] == "", "signup/OAuth пишет plaintext в users.api_key"
        assert row["api_key_hash"] == _digest(raw)

        new_key = _uniq("roma-key-new")
        result = db.rotate_user_api_key(user_id, tenant_id, new_key)
        assert result["users_count"] == 1 and result["tenants_count"] == 1

        row = _row(user_id)
        assert row["api_key"] == "", "ротация пишет plaintext в users.api_key"
        assert row["api_key_hash"] == _digest(new_key)
    finally:
        _delete_users(created)
