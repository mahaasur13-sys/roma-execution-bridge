"""TXN-CONTEXT-UNIT-TESTS: offline regression tests for _PgConnection/get_connection.

Scope (patched behaviour only):

* ``_PgConnection.__exit__`` / ``_finish`` transaction contract:
  commit only on a clean body exit, rollback on a raised body, commit failure
  is re-raised and the connection is discarded instead of being returned to
  the pool as healthy, cleanup runs exactly once and never masks the primary
  error.
* ``PGConnectionManager.get_connection`` ping-failure path: the borrowed
  connection must not leak the pool slot.

Offline only: no psycopg2.connect, no DSN from the environment, no network.
Every pool/connection/cursor here is a local fake.
"""

from __future__ import annotations

import pytest

import billing.pg_connection as pgconn
from billing.pg_connection import PGUnavailableError, PGConnectionManager, _PgConnection

FAKE_DSN = "postgresql://user:pw@127.0.0.1:5432/roma_test"


class FakeCursor:
    def __init__(self, owner, fail_execute=False, fail_close=False, row=("PostgreSQL 15.4",)):
        self.owner = owner
        self.executed: list[str] = []
        self.closed = 0
        self._fail_execute = fail_execute
        self._fail_close = fail_close
        self._row = row

    def execute(self, sql):
        self.executed.append(sql)
        if self._fail_execute:
            raise RuntimeError("ping execute failed")

    def fetchone(self):
        return self._row

    def close(self):
        self.closed += 1
        if self._fail_close:
            raise RuntimeError("cursor close failed")


class FakeConn:
    """Records driver calls; can be made to fail on any single operation."""

    def __init__(
        self,
        *,
        commit_error: Exception | None = None,
        rollback_error: Exception | None = None,
        close_error: Exception | None = None,
        cursor_error: Exception | None = None,
        fail_execute: bool = False,
        fail_cursor_close: bool = False,
        row=("PostgreSQL 15.4",),
    ):
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.cursors: list[FakeCursor] = []
        self._commit_error = commit_error
        self._rollback_error = rollback_error
        self._close_error = close_error
        self._cursor_error = cursor_error
        self._fail_execute = fail_execute
        self._fail_cursor_close = fail_cursor_close
        self._row = row

    def cursor(self):
        if self._cursor_error is not None:
            raise self._cursor_error
        cur = FakeCursor(self, fail_execute=self._fail_execute, fail_close=self._fail_cursor_close, row=self._row)
        self.cursors.append(cur)
        return cur

    def commit(self):
        self.commits += 1
        if self._commit_error is not None:
            raise self._commit_error

    def rollback(self):
        self.rollbacks += 1
        if self._rollback_error is not None:
            raise self._rollback_error

    def close(self):
        self.closes += 1
        if self._close_error is not None:
            raise self._close_error


class FakePool:
    """Minimal pool double: hands out prepared connections, records putconn."""

    def __init__(self, conns=None, putconn_error: Exception | None = None):
        self._conns = list(conns or [])
        self.getconn_calls = 0
        self.putconn_calls: list[tuple[object, bool]] = []
        self._putconn_error = putconn_error

    def getconn(self):
        self.getconn_calls += 1
        if not self._conns:
            raise RuntimeError("pool exhausted")
        return self._conns.pop(0)

    def putconn(self, conn, close=False):
        self.putconn_calls.append((conn, close))
        if self._putconn_error is not None:
            raise self._putconn_error


@pytest.fixture(autouse=True)
def no_real_pool(monkeypatch):
    """Backstop: no test in this module may construct a real connection pool."""
    import psycopg2.pool as pgpool

    attempts: list[tuple] = []

    def _forbidden(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("offline unit test attempted to create a real PG pool")

    monkeypatch.setattr(pgpool, "ThreadedConnectionPool", _forbidden)
    yield attempts
    assert attempts == [], "real pool creation attempted in an offline unit test"


@pytest.fixture()
def offline(monkeypatch):
    """Force the module into the 'PG configured' branch without any real DSN.

    Only module-level constants are patched; the manager always receives a fake
    pool and a fake ``_create_pool``, so ``psycopg2`` is never touched.
    """
    monkeypatch.setattr(pgconn, "PG_DSN", FAKE_DSN)
    monkeypatch.setattr(pgconn, "PG_RECONNECT_ATTEMPTS", 1)
    monkeypatch.setattr(pgconn, "PG_RETRY_BASE_DELAY", 0.0)
    return pgconn


def make_manager(pool, *, available=True) -> PGConnectionManager:
    """Manager bound to a fake pool; ``_create_pool`` re-arms the fake pool.

    ``get_connection`` clears ``_pg_available`` after a failed ping and then asks
    ``_ensure_pool()`` for a pool again. Without this stub that second call would
    build a real pool against the (fake) DSN, so the stub is what keeps the test
    offline — it is not a convenience.
    """
    mgr = PGConnectionManager()
    mgr._pool = pool
    mgr._pg_available = available

    def _fake_create_pool() -> bool:
        mgr._pool = pool
        mgr._pg_available = True
        return True

    mgr._create_pool = _fake_create_pool
    return mgr


# --- 1. normal exit ---------------------------------------------------------


def test_normal_exit_commits_once_and_returns_conn_to_pool(offline):
    conn = FakeConn()
    pool = FakePool([conn])
    mgr = make_manager(pool)

    with mgr.get_connection("query") as borrowed:
        assert borrowed is conn

    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert conn.closes == 0
    assert pool.putconn_calls == [(conn, False)]


# --- 2. body exception ------------------------------------------------------


def test_body_exception_skips_commit_rolls_back_and_propagates(offline):
    conn = FakeConn()
    pool = FakePool([conn])
    mgr = make_manager(pool)
    boom = ValueError("body failed")

    with pytest.raises(ValueError) as excinfo:
        with mgr.get_connection("query"):
            raise boom

    assert excinfo.value is boom
    assert conn.commits == 0
    assert conn.rollbacks == 1
    assert pool.putconn_calls == [(conn, False)]


# --- 3. rollback failure while unwinding ------------------------------------


def test_rollback_failure_preserves_body_exception_and_discards_conn(offline):
    conn = FakeConn(rollback_error=RuntimeError("rollback failed"))
    pool = FakePool([conn])
    mgr = make_manager(pool)
    boom = ValueError("body failed")

    with pytest.raises(ValueError) as excinfo:
        with mgr.get_connection("query"):
            raise boom

    assert excinfo.value is boom
    assert conn.commits == 0
    assert conn.rollbacks == 1
    assert pool.putconn_calls == [(conn, True)]
    assert conn.closes == 0


# --- 4. commit failure ------------------------------------------------------


def test_commit_failure_propagates_and_discards_conn(offline):
    conn = FakeConn(commit_error=RuntimeError("commit failed"))
    pool = FakePool([conn])
    mgr = make_manager(pool)

    with pytest.raises(RuntimeError, match="commit failed"):
        with mgr.get_connection("query"):
            pass

    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert pool.putconn_calls == [(conn, True)]


# --- 5. putconn failure fallback -------------------------------------------


def test_putconn_failure_falls_back_to_close(offline):
    conn = FakeConn()
    pool = FakePool([conn], putconn_error=RuntimeError("putconn failed"))
    mgr = make_manager(pool)

    with mgr.get_connection("query"):
        pass

    assert conn.commits == 1
    assert len(pool.putconn_calls) == 1
    assert conn.closes == 1


def test_commit_failure_with_putconn_failure_keeps_commit_error(offline):
    conn = FakeConn(commit_error=RuntimeError("commit failed"))
    pool = FakePool([conn], putconn_error=RuntimeError("putconn failed"))
    mgr = make_manager(pool)

    with pytest.raises(RuntimeError, match="commit failed"):
        with mgr.get_connection("query"):
            pass

    assert conn.closes == 1
    assert len(pool.putconn_calls) == 1


# --- 6. exactly-once cleanup ------------------------------------------------


def test_cleanup_runs_exactly_once_on_repeated_exit(offline):
    conn = FakeConn()
    pool = FakePool([conn])
    mgr = make_manager(pool)
    ctx = _PgConnection(mgr, conn)

    assert ctx.__exit__(None, None, None) is False
    assert ctx.__exit__(None, None, None) is False

    assert conn.commits == 1
    assert len(pool.putconn_calls) == 1


def test_release_is_idempotent(offline):
    conn = FakeConn()
    pool = FakePool([conn])
    mgr = make_manager(pool)
    ctx = _PgConnection(mgr, conn)

    ctx._release()
    ctx._release(close=True)

    assert pool.putconn_calls == [(conn, False)]


# --- 7. explicit transaction statements inside the body ---------------------


def test_explicit_txn_commit_in_body_keeps_wrapper_contract(offline):
    conn = FakeConn()
    pool = FakePool([conn])
    mgr = make_manager(pool)

    with mgr.get_connection("txn") as borrowed:
        cur = borrowed.cursor()
        cur.execute("BEGIN")
        cur.execute("INSERT INTO ledger_debits VALUES (1)")
        cur.execute("COMMIT")
        cur.close()

    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert pool.putconn_calls == [(conn, False)]


def test_explicit_txn_rollback_in_body_keeps_primary_error(offline):
    conn = FakeConn()
    pool = FakePool([conn])
    mgr = make_manager(pool)
    boom = RuntimeError("txn body failed")

    with pytest.raises(RuntimeError) as excinfo:
        with mgr.get_connection("txn") as borrowed:
            cur = borrowed.cursor()
            cur.execute("BEGIN")
            cur.execute("ROLLBACK")
            raise boom

    assert excinfo.value is boom
    assert conn.commits == 0
    assert conn.rollbacks == 1
    assert pool.putconn_calls == [(conn, False)]


# --- 8. ping failure in get_connection --------------------------------------


def test_ping_failure_releases_pool_slot_with_close(offline, monkeypatch):
    monkeypatch.setattr(pgconn, "PG_RECONNECT_ATTEMPTS", 0)
    conn = FakeConn(fail_execute=True)
    pool = FakePool([conn])
    mgr = make_manager(pool)

    with pytest.raises(PGUnavailableError):
        mgr.get_connection("query")

    assert pool.putconn_calls == [(conn, True)]
    assert conn.closes == 0


def test_ping_failure_retries_then_raises(offline):
    conns = [FakeConn(fail_execute=True), FakeConn(fail_execute=True)]
    pool = FakePool(list(conns))
    mgr = make_manager(pool)

    with pytest.raises(PGUnavailableError):
        mgr.get_connection("query")

    assert pool.putconn_calls == [(conns[0], True), (conns[1], True)]
    assert mgr._error_count == 2
    assert mgr._reconnect_count == 1
    assert mgr._pg_available is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cursor_error": RuntimeError("cursor failed")},
        {"fail_execute": True},
        {"fail_cursor_close": True},
    ],
    ids=["cursor", "execute", "cursor-close"],
)
def test_ping_failure_branches_release_connection(offline, monkeypatch, kwargs):
    monkeypatch.setattr(pgconn, "PG_RECONNECT_ATTEMPTS", 0)
    conn = FakeConn(**kwargs)
    pool = FakePool([conn])
    mgr = make_manager(pool)

    with pytest.raises(PGUnavailableError):
        mgr.get_connection("query")

    assert pool.putconn_calls == [(conn, True)]
    assert mgr._error_count == 1
    assert mgr._reconnect_count == 0


def test_pool_exhaustion_releases_nothing_and_raises(offline, monkeypatch):
    monkeypatch.setattr(pgconn, "PG_RECONNECT_ATTEMPTS", 0)
    pool = FakePool([])
    mgr = make_manager(pool)

    with pytest.raises(PGUnavailableError):
        mgr.get_connection("query")

    assert pool.putconn_calls == []
    assert mgr._pg_available is False


def test_putconn_failure_after_failed_ping_falls_back_to_close(offline, monkeypatch):
    monkeypatch.setattr(pgconn, "PG_RECONNECT_ATTEMPTS", 0)
    conn = FakeConn(fail_execute=True)
    pool = FakePool([conn], putconn_error=RuntimeError("putconn failed"))
    mgr = make_manager(pool)

    with pytest.raises(PGUnavailableError):
        mgr.get_connection("query")

    assert len(pool.putconn_calls) == 1
    assert conn.closes == 1


# --- 9. health() semantics --------------------------------------------------


def test_health_disabled_without_dsn(offline, monkeypatch):
    monkeypatch.setattr(pgconn, "PG_DSN", "")
    mgr = PGConnectionManager()

    info = mgr.health()

    assert info["status"] == "disabled"
    assert info["connected"] is False


def test_health_unavailable_when_pool_cannot_be_created(offline, monkeypatch):
    mgr = make_manager(FakePool([]), available=False)
    monkeypatch.setattr(mgr, "_ensure_pool", lambda: False)

    info = mgr.health()

    assert info["status"] == "unavailable"
    assert info["connected"] is False


def test_health_healthy_reports_version(offline):
    conn = FakeConn(row=("PostgreSQL 15.4 on x86_64-pc-linux-gnu",))
    pool = FakePool([conn])
    mgr = make_manager(pool)

    info = mgr.health()

    assert info["status"] == "healthy"
    assert info["connected"] is True
    assert info["version"] == "PostgreSQL 15.4 on x86_64-pc-linux-gnu"


def test_health_ping_failure_is_unreachable_not_healthy(offline, monkeypatch):
    monkeypatch.setattr(pgconn, "PG_RECONNECT_ATTEMPTS", 0)
    pool = FakePool([FakeConn(fail_execute=True)])
    mgr = make_manager(pool)

    info = mgr.health()

    assert info["status"] == "unreachable"
    assert info["connected"] is False


def test_health_degraded_on_non_pg_error_inside_body(offline):
    conn = FakeConn(commit_error=RuntimeError("commit failed during health"))
    pool = FakePool([conn])
    mgr = make_manager(pool)

    info = mgr.health()

    assert info["status"] == "degraded"
    assert "commit failed during health" in info["error"]


# --- 10. F1/F2/F3 regression cases ------------------------------------------
#
# F1  get_connection(): the ping cursor must be closed when the probe fails.
# F2  get_connection(): the borrowed connection must be released to the pool
#     that lent it, not to a pool that a concurrent _create_pool() installed.
# F3  _PgConnection.__exit__(): the active-connections gauge must be
#     decremented exactly once per borrow, even on a repeated __exit__.


class FakeGauge:
    """Counting stand-in for the prometheus active-connections gauge."""

    def __init__(self):
        self.inc_calls = 0
        self.dec_calls = 0

    def labels(self, **kwargs):
        return self

    def inc(self):
        self.inc_calls += 1

    def dec(self):
        self.dec_calls += 1


# --- F1 ---------------------------------------------------------------------


def test_f1_ping_failure_closes_probe_cursor(offline, monkeypatch):
    monkeypatch.setattr(pgconn, "PG_RECONNECT_ATTEMPTS", 0)
    conn = FakeConn(fail_execute=True)
    pool = FakePool([conn])
    mgr = make_manager(pool)

    with pytest.raises(PGUnavailableError) as excinfo:
        mgr.get_connection("query")

    assert len(conn.cursors) == 1
    assert conn.cursors[0].closed == 1, "probe cursor leaked on the failure path"
    assert "ping execute failed" in str(excinfo.value)
    assert pool.putconn_calls == [(conn, True)]


def test_f1_ping_failure_closes_every_attempt_cursor(offline):
    conns = [FakeConn(fail_execute=True), FakeConn(fail_execute=True)]
    pool = FakePool(list(conns))
    mgr = make_manager(pool)

    with pytest.raises(PGUnavailableError):
        mgr.get_connection("query")

    cursors = [cur for conn in conns for cur in conn.cursors]
    assert len(cursors) == 2
    assert all(cur.closed == 1 for cur in cursors)


def test_f1_cursor_close_failure_does_not_mask_probe_error(offline, monkeypatch):
    monkeypatch.setattr(pgconn, "PG_RECONNECT_ATTEMPTS", 0)
    conn = FakeConn(fail_execute=True, fail_cursor_close=True)
    pool = FakePool([conn])
    mgr = make_manager(pool)

    with pytest.raises(PGUnavailableError) as excinfo:
        mgr.get_connection("query")

    assert conn.cursors[0].closed == 1
    assert "ping execute failed" in str(excinfo.value)
    assert "cursor close failed" not in str(excinfo.value)


# --- F2 ---------------------------------------------------------------------


def test_f2_failed_ping_returns_conn_to_lending_pool(offline, monkeypatch):
    monkeypatch.setattr(pgconn, "PG_RECONNECT_ATTEMPTS", 0)
    conn = FakeConn(fail_execute=True)
    lending = FakePool([conn])
    replacement = FakePool([])
    mgr = make_manager(lending)

    original_getconn = lending.getconn

    def getconn_then_swap():
        borrowed = original_getconn()
        mgr._pool = replacement  # a concurrent _create_pool() swap
        return borrowed

    lending.getconn = getconn_then_swap

    with pytest.raises(PGUnavailableError):
        mgr.get_connection("query")

    assert lending.putconn_calls == [(conn, True)]
    assert replacement.putconn_calls == []
    assert mgr._pool is replacement


# --- F3 ---------------------------------------------------------------------


def test_f3_gauge_balanced_across_a_full_cycle(offline, monkeypatch):
    gauge = FakeGauge()
    monkeypatch.setattr(pgconn, "_pg_conn_active", gauge)
    conn = FakeConn()
    pool = FakePool([conn])
    mgr = make_manager(pool)

    ctx = mgr.get_connection("query")
    assert gauge.inc_calls == 1

    assert ctx.__exit__(None, None, None) is False

    assert conn.commits == 1
    assert gauge.dec_calls == 1
    assert gauge.dec_calls <= gauge.inc_calls


def test_f3_repeated_exit_decrements_gauge_once(offline, monkeypatch):
    gauge = FakeGauge()
    monkeypatch.setattr(pgconn, "_pg_conn_active", gauge)
    conn = FakeConn()
    pool = FakePool([conn])
    mgr = make_manager(pool)

    ctx = mgr.get_connection("query")
    ctx.__exit__(None, None, None)
    ctx.__exit__(None, None, None)

    assert conn.commits == 1
    assert len(pool.putconn_calls) == 1
    assert gauge.inc_calls == 1
    assert gauge.dec_calls == 1, "gauge decremented more than once for one borrow"


def test_f3_gauge_balanced_on_body_exception(offline, monkeypatch):
    gauge = FakeGauge()
    monkeypatch.setattr(pgconn, "_pg_conn_active", gauge)
    conn = FakeConn()
    pool = FakePool([conn])
    mgr = make_manager(pool)

    ctx = mgr.get_connection("query")
    error = ValueError("body boom")
    assert ctx.__exit__(type(error), error, None) is False

    assert conn.rollbacks == 1
    assert conn.commits == 0
    assert gauge.inc_calls == 1
    assert gauge.dec_calls == 1


def test_f3_gauge_balanced_on_commit_failure(offline, monkeypatch):
    gauge = FakeGauge()
    monkeypatch.setattr(pgconn, "_pg_conn_active", gauge)
    conn = FakeConn(commit_error=RuntimeError("commit boom"))
    pool = FakePool([conn])
    mgr = make_manager(pool)

    ctx = mgr.get_connection("query")
    with pytest.raises(RuntimeError, match="commit boom"):
        ctx.__exit__(None, None, None)

    assert pool.putconn_calls == [(conn, True)]
    assert gauge.inc_calls == 1
    assert gauge.dec_calls == 1


def test_f3_no_gauge_drift_when_borrow_fails(offline, monkeypatch):
    gauge = FakeGauge()
    monkeypatch.setattr(pgconn, "_pg_conn_active", gauge)
    monkeypatch.setattr(pgconn, "PG_RECONNECT_ATTEMPTS", 0)
    conn = FakeConn(fail_execute=True)
    pool = FakePool([conn])
    mgr = make_manager(pool)

    with pytest.raises(PGUnavailableError):
        mgr.get_connection("query")

    assert gauge.inc_calls == 0
    assert gauge.dec_calls == 0


# --- 11. R-01 pool ownership -------------------------------------------------
#
# R-01  _PgConnection._release(): the borrowed connection must be returned to
#       the pool that lent it. ``_release`` used to re-read ``mgr._pool`` at
#       release time, so a concurrent ``_create_pool()`` that swapped the pool
#       while the connection was checked out sent the borrow to the *new* pool
#       and leaked the slot on the lending one.
#
# Every case below borrows from pool A, swaps ``mgr._pool`` to pool B inside the
# ``with`` body, and asserts that only A ever sees the connection.


class PoolError(Exception):
    """Mirrors psycopg2.pool.PoolError raised by putconn on a closed pool."""


class ClosedLendingPool(FakePool):
    """Lender that was closed by a concurrent ``_create_pool()`` swap."""

    def putconn(self, conn, close=False):
        self.putconn_calls.append((conn, close))
        raise PoolError("attempting to put connection to already closed connection pool")


def swap_pool_inside_body(mgr, replacement):
    """Return a helper that installs ``replacement`` as ``mgr._pool`` once."""
    swapped = {"done": False}

    def _swap():
        if not swapped["done"]:
            mgr._pool = replacement
            swapped["done"] = True

    return _swap


def test_r01_success_release_returns_conn_to_lending_pool_after_swap(offline):
    conn = FakeConn()
    lending = FakePool([conn])
    replacement = FakePool([])
    mgr = make_manager(lending)
    swap = swap_pool_inside_body(mgr, replacement)

    with mgr.get_connection("query") as borrowed:
        assert borrowed is conn
        swap()

    assert lending.putconn_calls == [(conn, False)]
    assert replacement.putconn_calls == []
    assert mgr._pool is replacement


def test_r01_release_does_not_reread_manager_pool(offline):
    conn = FakeConn()
    lending = FakePool([conn])
    mgr = make_manager(lending)

    with mgr.get_connection("query"):
        # Worst case: the manager no longer has any pool at all. A release that
        # re-read the manager would hit the "PG pool is gone" branch and close
        # the connection; the borrow must still go back to its lender.
        mgr._pool = None

    assert lending.putconn_calls == [(conn, False)]
    assert conn.closes == 0


def test_r01_commit_failure_release_uses_lending_pool(offline):
    conn = FakeConn(commit_error=RuntimeError("commit failed"))
    lending = FakePool([conn])
    replacement = FakePool([])
    mgr = make_manager(lending)
    swap = swap_pool_inside_body(mgr, replacement)

    with pytest.raises(RuntimeError, match="commit failed"):
        with mgr.get_connection("query"):
            swap()

    assert lending.putconn_calls == [(conn, True)]
    assert replacement.putconn_calls == []
    assert conn.commits == 1


def test_r01_body_exception_release_uses_lending_pool(offline):
    conn = FakeConn()
    lending = FakePool([conn])
    replacement = FakePool([])
    mgr = make_manager(lending)
    swap = swap_pool_inside_body(mgr, replacement)
    boom = ValueError("body failed")

    with pytest.raises(ValueError) as excinfo:
        with mgr.get_connection("query"):
            swap()
            raise boom

    assert excinfo.value is boom
    assert conn.commits == 0
    assert conn.rollbacks == 1
    assert lending.putconn_calls == [(conn, False)]
    assert replacement.putconn_calls == []


def test_r01_rollback_failure_release_uses_lending_pool(offline):
    conn = FakeConn(rollback_error=RuntimeError("rollback failed"))
    lending = FakePool([conn])
    replacement = FakePool([])
    mgr = make_manager(lending)
    swap = swap_pool_inside_body(mgr, replacement)
    boom = ValueError("body failed")

    with pytest.raises(ValueError) as excinfo:
        with mgr.get_connection("query"):
            swap()
            raise boom

    assert excinfo.value is boom
    assert lending.putconn_calls == [(conn, True)]
    assert replacement.putconn_calls == []


def test_r01_lending_putconn_failure_closes_conn_not_other_pool(offline):
    conn = FakeConn()
    lending = FakePool([conn], putconn_error=RuntimeError("putconn failed"))
    replacement = FakePool([])
    mgr = make_manager(lending)
    swap = swap_pool_inside_body(mgr, replacement)

    with mgr.get_connection("query"):
        swap()

    assert len(lending.putconn_calls) == 1
    assert conn.closes == 1
    assert replacement.putconn_calls == []


def test_r01_closed_lending_pool_does_not_receive_conn(offline):
    conn = FakeConn()
    lending = ClosedLendingPool([conn])
    replacement = FakePool([])
    mgr = make_manager(lending)
    swap = swap_pool_inside_body(mgr, replacement)

    with mgr.get_connection("query"):
        swap()

    assert len(lending.putconn_calls) == 1
    assert conn.closes == 1, "connection must be closed, not handed to the new pool"
    assert replacement.putconn_calls == []


def test_r01_lender_captured_for_two_arg_wrapper(offline):
    conn = FakeConn()
    lending = FakePool([conn])
    replacement = FakePool([])
    mgr = make_manager(lending)

    ctx = _PgConnection(mgr, conn)  # legacy two-argument construction
    mgr._pool = replacement

    assert ctx.__exit__(None, None, None) is False

    assert lending.putconn_calls == [(conn, False)]
    assert replacement.putconn_calls == []


def test_r01_exactly_once_bookkeeping_after_swap(offline, monkeypatch):
    gauge = FakeGauge()
    monkeypatch.setattr(pgconn, "_pg_conn_active", gauge)
    conn = FakeConn()
    lending = FakePool([conn])
    replacement = FakePool([])
    mgr = make_manager(lending)

    ctx = mgr.get_connection("query")
    assert gauge.inc_calls == 1
    mgr._pool = replacement

    assert ctx.__exit__(None, None, None) is False
    assert ctx.__exit__(None, None, None) is False

    assert conn.commits == 1
    assert len(lending.putconn_calls) == 1
    assert replacement.putconn_calls == []
    assert ctx._returned is True
    assert gauge.inc_calls == 1
    assert gauge.dec_calls == 1, "gauge decremented more than once for one borrow"


def test_r01_ping_failure_still_uses_lending_pool_after_swap(offline, monkeypatch):
    monkeypatch.setattr(pgconn, "PG_RECONNECT_ATTEMPTS", 0)
    conn = FakeConn(fail_execute=True)
    lending = FakePool([conn])
    replacement = FakePool([])
    mgr = make_manager(lending)

    original_getconn = lending.getconn

    def getconn_then_swap():
        borrowed = original_getconn()
        mgr._pool = replacement
        return borrowed

    lending.getconn = getconn_then_swap

    with pytest.raises(PGUnavailableError):
        mgr.get_connection("query")

    assert lending.putconn_calls == [(conn, True)]
    assert replacement.putconn_calls == []
