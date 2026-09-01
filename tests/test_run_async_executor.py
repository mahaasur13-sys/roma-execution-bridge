"""D1: db_adapter._run_async reuses one process-wide ThreadPoolExecutor."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_adapter


def test_run_async_reuses_executor():
    async def _ok():
        return 42

    async def _drive():
        # Inside a running loop, _run_async delegates to the shared executor.
        return db_adapter._run_async(_ok()), db_adapter._run_async(_ok())

    executor = db_adapter._ASYNC_EXECUTOR
    a, b = asyncio.run(_drive())
    assert (a, b) == (42, 42)

    # Same module-level executor object across calls — not a fresh per-call pool.
    assert db_adapter._ASYNC_EXECUTOR is executor

    # Still alive: the old `with ThreadPoolExecutor(...)` would shut it down.
    assert executor.submit(lambda: 1).result() == 1
