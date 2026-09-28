"""G-SEC4/M1: sqlite legacy plaintext api_key → hash backfill in init_db().

Context (INCIDENT ZO-SEC2 / ZO-SEC3): plaintext tenant keys must not survive a
fresh init on the dev/test sqlite path when they carry no hash. ``init_db()``
must backfill ``sha256(api_key)`` once and clear the plaintext, idempotently,
and only on sqlite (the PG path is authoritative and backfilled separately).
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import db

LEGACY_KEY = "legacy-plain-key-abc"


def _old_schema_sqlite(path: Path) -> None:
    """Create a tenants table shaped like the pre-G-SEC4 schema (no api_key_hash)."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE tenants (
            id TEXT PRIMARY KEY,
            api_key TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            plan TEXT NOT NULL DEFAULT 'free',
            subscription_status TEXT NOT NULL DEFAULT 'inactive'
        );
        """
    )
    conn.execute(
        "INSERT INTO tenants (id, api_key, name) VALUES (?, ?, ?)",
        ("tenant-legacy", LEGACY_KEY, "Legacy"),
    )
    conn.commit()
    conn.close()


def _use_tmp_db(monkeypatch, tmp_path: Path) -> Path:
    path = tmp_path / "roma.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


def test_legacy_plaintext_row_is_hashed_and_cleared(monkeypatch, tmp_path):
    path = _use_tmp_db(monkeypatch, tmp_path)
    _old_schema_sqlite(path)

    db.init_db()

    conn = sqlite3.connect(str(path))
    row = conn.execute(
        "SELECT api_key, api_key_hash FROM tenants WHERE id = ?", ("tenant-legacy",)
    ).fetchone()
    conn.close()

    expected = hashlib.sha256(LEGACY_KEY.encode("utf-8")).hexdigest()
    assert row[0] == ""
    assert row[1] == expected


def test_init_db_is_idempotent(monkeypatch, tmp_path):
    path = _use_tmp_db(monkeypatch, tmp_path)
    _old_schema_sqlite(path)

    db.init_db()
    db.init_db()

    conn = sqlite3.connect(str(path))
    rows = conn.execute(
        "SELECT api_key, api_key_hash FROM tenants WHERE id = ?", ("tenant-legacy",)
    ).fetchall()
    conn.close()

    expected = hashlib.sha256(LEGACY_KEY.encode("utf-8")).hexdigest()
    assert rows == [("", expected)]
