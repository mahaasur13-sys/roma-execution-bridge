"""P1: атомарный claim queued-джоб (claim_queued_execution_jobs).

Каждая queued-джоба достаётся ровно одной реплике либо никому. SQLite-тесты
проверяют базовую логику; PG-тест (маркер `pg`, env-skip без PG_DSN) гоняет
две независимые реплики-процесса с отдельными пулами соединений и барьером.
"""

import asyncio
import multiprocessing as mp
import os
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


# ── PG: две реплики, барьер, много итераций ─────────────────────────────────

ITER = int(os.environ.get("CLAIM_PG_ITER", "200"))
JOBS_PER_ITER = 3
TENANT = "t-atomic-claim"


def _replica(name, dsn, barrier, jobq, resq):
    os.environ["PG_DSN"] = dsn
    sys.path.insert(0, REPO)
    os.chdir(REPO)
    import db_adapter as db

    while True:
        token = jobq.get()
        if token is None:
            return
        barrier.wait()
        claimed = [j["id"] for j in db.claim_queued_execution_jobs(limit=5) if j["tenant_id"] == TENANT]
        resq.put((name, token, claimed))


@pytest.mark.pg  # G-CI-PG-CANON: env-skip без живого PG
def test_pg_two_replicas_never_double_claim():
    dsn = os.environ.get("PG_DSN")
    if not dsn:
        pytest.skip("PG_DSN не задан; issue: G-CI-PG-CANON · expiry: 2026-12-31")
    psycopg2 = pytest.importorskip("psycopg2")
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(2)
    qa, qb, res = ctx.Queue(), ctx.Queue(), ctx.Queue()
    procs = [ctx.Process(target=_replica, args=(n, dsn, barrier, q, res)) for n, q in (("A", qa), ("B", qb))]
    for p in procs:
        p.start()
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DELETE FROM execution_jobs WHERE tenant_id=%s", (TENANT,))
    double = lost = 0
    claimed_total = inserted_total = 0
    try:
        for i in range(ITER):
            ids = [f"claim-{i}-{k}-{uuid.uuid4().hex[:8]}" for k in range(JOBS_PER_ITER)]
            for jid in ids:
                cur.execute(
                    "INSERT INTO execution_jobs (id, tenant_id, status, backend) VALUES (%s,%s,'queued','local')",
                    (jid, TENANT),
                )
            inserted_total += len(ids)
            qa.put(i)
            qb.put(i)
            got = [res.get(timeout=60), res.get(timeout=60)]
            a = set(got[0][2]) & set(ids)
            b = set(got[1][2]) & set(ids)
            double += len(a & b)
            lost += len(set(ids) - (a | b))
            claimed_total += len(a) + len(b)
            cur.execute("SELECT count(*) FROM execution_jobs WHERE id = ANY(%s) AND status='running'", (ids,))
            assert cur.fetchone()[0] == len(ids)
            cur.execute("UPDATE execution_jobs SET status='completed' WHERE id = ANY(%s)", (ids,))
    finally:
        qa.put(None)
        qb.put(None)
        for p in procs:
            p.join(15)
            if p.is_alive():
                p.terminate()
        cur.execute("DELETE FROM execution_jobs WHERE tenant_id=%s", (TENANT,))
        conn.close()
    print(f"\nCLAIM iterations={ITER} jobs={inserted_total} claimed={claimed_total} double={double} lost={lost}")
    assert double == 0, f"двойной захват: {double} джоб из {inserted_total}"
    assert claimed_total == inserted_total and lost == 0
