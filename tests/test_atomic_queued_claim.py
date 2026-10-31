"""P1: атомарный claim queued-джоб (claim_queued_execution_jobs).

Каждая queued-джоба достаётся ровно одной реплике либо никому. SQLite-тесты
проверяют базовую логику; PG-тест (маркер `pg`, env-skip без PG_DSN) гоняет
две независимые реплики-процесса с отдельными пулами соединений и барьером.
"""

import asyncio
import multiprocessing as mp
import os
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

import db_adapter

REPO = str(Path(__file__).resolve().parents[1])


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.setattr(db_adapter, "_USE_PG", False)
    monkeypatch.setattr(db_adapter, "DB_PATH", tmp_path / "claim.db")
    return db_adapter


def _insert(db, jid, status="queued", created="2026-01-01 00:00:00"):
    c = db._sqlite_conn()
    try:
        db._ensure_execution_jobs_table(c)
        c.execute(
            "INSERT INTO execution_jobs (id, tenant_id, status, payload, created_at) VALUES (?,?,?,?,?)",
            (jid, "t-claim", status, '{"k": 1}', created),
        )
        c.commit()
    finally:
        c.close()


def _status(db, jid):
    return db.get_execution_job(jid)


def test_sqlite_second_claim_gets_nothing(sqlite_db):
    _insert(sqlite_db, "j1")
    first = sqlite_db.claim_queued_execution_jobs(limit=5)
    second = sqlite_db.claim_queued_execution_jobs(limit=5)
    assert [j["id"] for j in first] == ["j1"]
    assert first[0]["status"] == "running"
    assert first[0]["payload"] == {"k": 1}
    assert second == []
    row = _status(sqlite_db, "j1")
    assert row["status"] == "running" and row["started_at"]


def test_sqlite_claim_only_queued_ordered_and_limited(sqlite_db):
    _insert(sqlite_db, "late", created="2026-01-03 00:00:00")
    _insert(sqlite_db, "early", created="2026-01-01 00:00:00")
    _insert(sqlite_db, "mid", created="2026-01-02 00:00:00")
    _insert(sqlite_db, "done", status="completed", created="2025-01-01 00:00:00")
    got = sqlite_db.claim_queued_execution_jobs(limit=2)
    assert [j["id"] for j in got] == ["early", "mid"]
    assert [j["id"] for j in sqlite_db.claim_queued_execution_jobs(limit=2)] == ["late"]
    assert _status(sqlite_db, "done")["status"] == "completed"


def test_poll_and_execute_runs_only_claimed_jobs(monkeypatch):
    """Воркер исполняет только то, что вернул claim, и не делает безусловный UPDATE running."""
    from billing import execution_worker as ew

    started, updates = [], []

    class FakeDB:
        def claim_queued_execution_jobs(self, limit):
            return [{"id": "c1", "tenant_id": "t", "payload": {"a": 1}}]

        def get_queued_execution_jobs(self, limit=10):  # pragma: no cover - не должен вызываться
            raise AssertionError("старый путь SELECT+UPDATE не должен использоваться")

        def update_execution_job(self, *a, **k):
            updates.append((a, k))

    async def fake_exec(jid, tid, payload):
        started.append((jid, tid, payload))

    class Stop(Exception):
        pass

    async def fake_sleep(_s):
        raise Stop

    monkeypatch.setattr(ew, "_db_adapter", FakeDB())
    monkeypatch.setattr(ew, "execute_and_bill", fake_exec)
    monkeypatch.setattr(ew, "sweep_orphans_once", lambda: 0)
    monkeypatch.setattr(ew.asyncio, "sleep", fake_sleep)

    async def run():
        with pytest.raises(Stop):
            await ew.poll_and_execute()
        await asyncio.gather(*[t for t in asyncio.all_tasks() if t is not asyncio.current_task()])

    asyncio.run(run())
    assert started == [("c1", "t", {"a": 1})]
    assert not any(k.get("status") == "running" for _a, k in updates)


def test_sqlite_claim_returns_fresh_timestamps(sqlite_db):
    """SQLite возвращает started_at/updated_at из БД после UPDATE, как PG RETURNING *."""
    _insert(sqlite_db, "ts1")
    c = sqlite_db._sqlite_conn()
    try:
        c.execute("UPDATE execution_jobs SET updated_at='2000-01-01 00:00:00', started_at=NULL WHERE id='ts1'")
        c.commit()
    finally:
        c.close()
    (got,) = sqlite_db.claim_queued_execution_jobs(limit=5)
    row = _status(sqlite_db, "ts1")
    assert got["started_at"] is not None and got["updated_at"] != "2000-01-01 00:00:00"
    assert got["started_at"] == row["started_at"]
    assert got["updated_at"] == row["updated_at"]
    assert got["status"] == row["status"] == "running"


def test_sqlite_claim_db_error_raises_and_rolls_back(sqlite_db, monkeypatch):
    """Ошибка БД не маскируется пустым списком; статус не меняется."""
    _insert(sqlite_db, "e1")
    real = sqlite_db._sqlite_conn

    class Conn:
        def __init__(self):
            self._c = real()

        def __getattr__(self, n):
            return getattr(self._c, n)

        def execute(self, sql, *a):
            if sql.lstrip().upper().startswith("UPDATE"):
                raise sqlite3.OperationalError("boom")
            return self._c.execute(sql, *a)

    monkeypatch.setattr(sqlite_db, "_sqlite_conn", Conn)
    with pytest.raises(sqlite3.OperationalError):
        sqlite_db.claim_queued_execution_jobs(limit=5)
    monkeypatch.setattr(sqlite_db, "_sqlite_conn", real)
    assert _status(sqlite_db, "e1")["status"] == "queued"


def test_pg_branch_db_error_raises_and_rolls_back(monkeypatch):
    """PG-ветка (мок соединения): ошибка execute → rollback и проброс, не []."""
    calls = []

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a):
            raise RuntimeError("pg down")

    class Conn:
        def cursor(self):
            return Cur()

        def commit(self):
            calls.append("commit")

        def rollback(self):
            calls.append("rollback")

    monkeypatch.setattr(db_adapter, "_pg_enabled", lambda: True)
    monkeypatch.setattr(db_adapter, "_pg_conn", lambda: Conn())
    monkeypatch.setattr(
        db_adapter, "_pg_return", lambda conn, close_on_error=False: calls.append(("ret", close_on_error))
    )
    with pytest.raises(RuntimeError, match="pg down"):
        db_adapter.claim_queued_execution_jobs(limit=5)
    assert calls == ["rollback", ("ret", True)]


def test_poll_and_execute_survives_claim_error(monkeypatch):
    """Исключение из claim логируется, цикл не падает и опрашивает снова."""
    from billing import execution_worker as ew

    n = {"claims": 0, "sleeps": 0}

    class FakeDB:
        def claim_queued_execution_jobs(self, limit):
            n["claims"] += 1
            raise RuntimeError("db down")

    class Stop(Exception):
        pass

    async def fake_sleep(_s):
        n["sleeps"] += 1
        if n["sleeps"] >= 2:
            raise Stop

    monkeypatch.setattr(ew, "_db_adapter", FakeDB())
    monkeypatch.setattr(ew, "sweep_orphans_once", lambda: 0)
    monkeypatch.setattr(ew.asyncio, "sleep", fake_sleep)
    warned = []
    monkeypatch.setattr(ew.logger, "warning", lambda msg, *a: warned.append(msg % a))
    with pytest.raises(Stop):
        asyncio.run(ew.poll_and_execute())
    assert n["claims"] == 2
    assert any("poll_and_execute.error" in w and "db down" in w for w in warned)


# ── PG: две реплики, барьер, много итераций ─────────────────────────────────

ITER = int(os.environ.get("CLAIM_PG_ITER", "200"))
JOBS_PER_ITER = 3


def _replica(name, dsn, tenant, barrier, jobq, resq):
    os.environ["PG_DSN"] = dsn
    sys.path.insert(0, REPO)
    os.chdir(REPO)
    import db_adapter as db

    while True:
        token = jobq.get()
        if token is None:
            return
        try:
            barrier.wait(timeout=60)
        except Exception:
            return
        claimed = [j["id"] for j in db.claim_queued_execution_jobs(limit=5) if j["tenant_id"] == tenant]
        resq.put((name, token, claimed))


def _stop_replicas(procs, queues, barrier):
    for q in queues:
        try:
            q.put(None)
        except Exception:
            pass
    try:
        barrier.abort()  # разбудить реплику, застрявшую на барьере
    except Exception:
        pass
    for p in procs:
        if p.pid is None:
            continue
        p.join(15)
        if p.is_alive():
            p.terminate()
            p.join(5)


def _run_two_replicas(dsn, iterations, connect_timeout=10):
    """Две реплики-процесса против PG. Любой ранний сбой (connect, подготовка)
    гарантированно останавливает процессы. Очистка — только по id этого запуска."""
    import psycopg2

    tenant = f"t-atomic-claim-{uuid.uuid4().hex}"
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(2)
    qa, qb, res = ctx.Queue(), ctx.Queue(), ctx.Queue()
    procs = [
        ctx.Process(target=_replica, args=(n, dsn, tenant, barrier, q, res), daemon=True)
        for n, q in (("A", qa), ("B", qb))
    ]
    created: list[str] = []
    conn = cur = None
    stats = {"double": 0, "lost": 0, "claimed": 0, "inserted": 0}
    try:
        for p in procs:
            p.start()
        conn = psycopg2.connect(dsn, connect_timeout=connect_timeout)
        conn.autocommit = True
        cur = conn.cursor()
        for i in range(iterations):
            ids = [f"claim-{i}-{k}-{uuid.uuid4().hex[:8]}" for k in range(JOBS_PER_ITER)]
            for jid in ids:
                cur.execute(
                    "INSERT INTO execution_jobs (id, tenant_id, status, backend) VALUES (%s,%s,'queued','local')",
                    (jid, tenant),
                )
                created.append(jid)
            stats["inserted"] += len(ids)
            qa.put(i)
            qb.put(i)
            got = [res.get(timeout=60), res.get(timeout=60)]
            a = set(got[0][2]) & set(ids)
            b = set(got[1][2]) & set(ids)
            stats["double"] += len(a & b)
            stats["lost"] += len(set(ids) - (a | b))
            stats["claimed"] += len(a) + len(b)
            cur.execute("SELECT count(*) FROM execution_jobs WHERE id = ANY(%s) AND status='running'", (ids,))
            assert cur.fetchone()[0] == len(ids)
            cur.execute("UPDATE execution_jobs SET status='completed' WHERE id = ANY(%s)", (ids,))
    finally:
        _stop_replicas(procs, (qa, qb), barrier)
        try:
            if cur is not None and created:
                cur.execute("DELETE FROM execution_jobs WHERE id = ANY(%s)", (created,))
        finally:
            if conn is not None:
                conn.close()
    return stats


@pytest.mark.pg  # G-CI-PG-CANON: env-skip без живого PG
def test_pg_two_replicas_never_double_claim():
    dsn = os.environ.get("PG_DSN")
    if not dsn:
        pytest.skip("PG_DSN не задан; issue: G-CI-PG-CANON · expiry: 2026-12-31")
    pytest.importorskip("psycopg2")
    st = _run_two_replicas(dsn, ITER)
    print(
        f"\nCLAIM iterations={ITER} jobs={st['inserted']} claimed={st['claimed']} "
        f"double={st['double']} lost={st['lost']}"
    )
    assert st["double"] == 0, f"двойной захват: {st['double']} джоб из {st['inserted']}"
    assert st["claimed"] == st["inserted"] and st["lost"] == 0


@pytest.mark.timeout(60)
def test_pg_harness_unreachable_dsn_cleans_up_processes():
    """Недоступный DSN: connect падает быстро, дочерние процессы остановлены, без зависания."""
    psycopg2 = pytest.importorskip("psycopg2")
    import time

    before = {p.pid for p in mp.active_children()}
    t0 = time.monotonic()
    with pytest.raises(psycopg2.OperationalError):
        _run_two_replicas("postgresql://u@/db?host=/nonexistent-claim-sock&connect_timeout=2", 1, connect_timeout=2)
    assert time.monotonic() - t0 < 45
    leftover = [p for p in mp.active_children() if p.pid not in before and p.is_alive()]
    assert leftover == []
