import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_get_pool_rejects_empty_dsn(monkeypatch):
    monkeypatch.delenv("PG_DSN", raising=False)
    fake = types.ModuleType("asyncpg")
    fake.Pool = object
    fake.create_pool = AsyncMock()
    monkeypatch.setitem(sys.modules, "asyncpg", fake)
    sys.modules.pop("db_pg", None)
    import db_pg
    db_pg.POOL = None
    monkeypatch.setattr(db_pg, "DEFAULT_DSN", "")
    with pytest.raises(RuntimeError, match="PG_DSN is required"):
        await db_pg.get_pool()
    fake.create_pool.assert_not_called()
