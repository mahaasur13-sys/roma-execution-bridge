"""Support Chat — SQLAlchemy engine/session factory (sync, psycopg2/SQLite).

Reuses the service-wide database: ``PG_DSN`` (``postgresql://`` URI) when set.
An explicit URL (tests/local) is always honoured. Without both, production
(``ENV=production`` / ``ROMA_ENV=production``) fails closed instead of silently
writing a local SQLite file; outside production the SQLite fallback is kept and
announced with a warning. The ORM models live in ``support_chat.db_models`` and
map 1:1 to migration ``support_chat/migrations/004_support_chat.sql`` tables.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.engine import Engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from support_chat.db_models import Base

_DEFAULT_SQLITE_URL = "sqlite:///support_chat.db"

logger = logging.getLogger(__name__)


class SupportDatabaseUnavailable(RuntimeError):
    """No support DB is available: no PG_DSN in production and no explicit DB."""


# The ORM models (db_models.py) use PostgreSQL-native JSONB/UUID. Make them
# render as portable equivalents on SQLite so local tests run without PG_DSN.
# This only affects SQLite DDL compilation; the models themselves stay untouched.
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(32)"


def _is_production() -> bool:
    """Repo-native production marker (same convention as env_loader/main.py)."""
    env = os.environ.get("ENV", "").strip().lower()
    roma_env = os.environ.get("ROMA_ENV", "").strip().lower()
    return env == "production" or roma_env == "production"


def _resolve_url(url: str | None) -> str:
    if url:
        return url
    dsn = os.environ.get("PG_DSN", "")
    if dsn:
        return dsn
    if _is_production():
        raise SupportDatabaseUnavailable(
            "support DB requires PG_DSN in production or an explicitly injected "
            "test/local DB"
        )
    logger.warning(
        "support DB: PG_DSN is unset outside production — falling back to a local "
        "SQLite file. Set PG_DSN for anything but tests/local."
    )
    return _DEFAULT_SQLITE_URL


def get_engine(url: str | None = None) -> Engine:
    resolved = _resolve_url(url)
    if resolved.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        if ":memory:" in resolved:
            return create_engine(
                resolved, connect_args=connect_args, poolclass=StaticPool
            )
        return create_engine(resolved, connect_args=connect_args)
    return create_engine(
        resolved, pool_pre_ping=True, connect_args={"connect_timeout": 5}
    )


def get_session_factory(url: str | None = None):
    return sessionmaker(bind=get_engine(url), expire_on_commit=False)


def init_db(engine: Engine | None = None) -> None:
    """Create support_chat tables if absent (idempotent, mirrors migration 004)."""
    engine = engine or get_engine()
    Base.metadata.create_all(engine)
