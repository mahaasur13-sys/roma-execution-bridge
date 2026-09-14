import os
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_get_pool_rejects_empty_dsn(monkeypatch):
    monkeypatch.delenv("PG_DSN", raising=False)
    import db_pg
    db_pg.POOL = None
    monkeypatch.setattr(db_pg, "DEFAULT_DSN", "")
    with patch("asyncpg.create_pool", new_callable=AsyncMock) as create_pool:
        with pytest.raises(RuntimeError, match="PG_DSN is required"):
            await db_pg.get_pool()
        create_pool.assert_not_called()
