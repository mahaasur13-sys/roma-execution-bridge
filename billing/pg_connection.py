"""ROMA Billing — PostgreSQL Connection Manager (fixed)."""

from __future__ import annotations
import os
import time
import threading
import logging
from typing import Optional

logger = logging.getLogger("roma.billing.pg")

PG_DSN = os.environ.get("PG_DSN", "")
PG_POOL_MIN = int(os.environ.get("PG_POOL_SIZE", "2"))
PG_POOL_MAX = int(os.environ.get("PG_MAX_OVERFLOW", "10"))
PG_POOL_TIMEOUT = int(os.environ.get("PG_POOL_TIMEOUT", "30"))
PG_RECONNECT_ATTEMPTS = int(os.environ.get("PG_RECONNECT_ATTEMPTS", "5"))
PG_RETRY_BASE_DELAY = 0.1

_pg_conn_active = None
_pg_reconnects_total = None
_pg_errors_total = None


def _init_metrics():
    global _pg_conn_active, _pg_reconnects_total, _pg_errors_total
    if _pg_conn_active is not None:
        return
    try:
        from prometheus_client import Gauge, Counter

        _pg_conn_active = Gauge("roma_pg_connections_active", "Active PG connections", ["pool"])
        _pg_reconnects_total = Counter("roma_pg_reconnects_total", "Total PG reconnection attempts", ["pool"])
        _pg_errors_total = Counter("roma_pg_errors_total", "Total PG query errors", ["pool", "error_type"])
    except ImportError:
        pass


class PGUnavailableError(Exception):
    """Raised when PostgreSQL is unreachable after all retries."""


class _PgConnection:
    """Class-based context manager: acquires conn, commits, returns to pool.

    Transaction contract:

    * normal body exit — ``commit()`` is attempted. A failed commit is never
      swallowed: the connection is discarded (unknown outcome) and the error
      propagates to the caller.
    * body raised — no commit is issued; a best-effort rollback keeps the
      connection consistent and the primary exception reaches the caller
      unchanged.
    * cleanup failures are logged and never mask the primary exception.
    * the pool slot is released exactly once on every path.
    """

    __slots__ = ("_mgr", "_conn", "_returned", "_pool")

    def __init__(self, mgr, conn, pool=None):
        self._mgr = mgr
        self._conn = conn
        self._returned = False
        # The pool that actually lent this connection is resolved once, at
        # borrow time, and never re-read from the manager: a concurrent
        # ``_create_pool()`` may install a different pool while the connection
        # is checked out, and the borrow must go back to its lender.
        self._pool = pool if pool is not None else getattr(mgr, "_pool", None)

    def __enter__(self):
        return self._conn

    def _release(self, close: bool = False) -> None:
        """Return the connection to the lending pool exactly once. Never raises.

        The lender is the pool captured when the connection was borrowed
        (``self._pool``), not whatever ``mgr._pool`` points at now. If the lender
        is gone or refuses the connection, the connection is closed rather than
        handed to a foreign pool.

        The active-connections gauge is decremented in the same step that
        releases the pool slot, so the decrement cannot run more often than the
        matching increment in ``get_connection()``.
        """
        if self._returned:
            return
        self._returned = True
        try:
            if self._conn is None:
                return
            pool = self._pool
            try:
                if pool is None:
                    raise RuntimeError("PG pool is gone")
                pool.putconn(self._conn, close=close)
            except Exception:
                logger.warning(
                    "PG putconn failed (close=%s); closing connection",
                    close,
                    exc_info=True,
                )
                try:
                    self._conn.close()
                except Exception:
                    logger.warning("PG connection close failed", exc_info=True)
        finally:
            if _pg_conn_active:
                _pg_conn_active.labels(pool="billing").dec()

    def _finish(self, exc_type) -> None:
        """Complete the transaction; a commit failure is re-raised.

        Every path ends in exactly one ``_release()`` call (or in none at all
        when the wrapper was already released) — the only statement that can
        raise here is the ``commit()`` below, and it runs *after* the release,
        so the gauge decrement always happens before the error escapes.
        """
        if self._conn is None or self._returned:
            return
        if exc_type is not None:
            # The body raised: never commit, never suppress the primary error.
            try:
                self._conn.rollback()
            except Exception:
                # The connection state is not trustworthy — discard it and
                # keep the original exception as the one the caller sees.
                logger.warning("PG rollback failed while unwinding an exception", exc_info=True)
                self._release(close=True)
                return
            self._release()
            return
        try:
            self._conn.commit()
        except Exception:
            # Unknown commit outcome: do not report success and do not hand
            # this connection back to the pool as healthy.
            logger.error("PG commit failed; discarding connection", exc_info=True)
            self._release(close=True)
            raise
        self._release()

    def __exit__(self, exc_type, exc, tb):
        self._finish(exc_type)
        return False


class PGConnectionManager:
    _instance: Optional[PGConnectionManager] = None
    _lock = threading.Lock()

    def __init__(self):
        self._pool = None
        self._pool_lock = threading.Lock()
        self._pg_available: bool | None = None
        self._reconnect_count: int = 0
        self._error_count: int = 0
        self._pool_min = PG_POOL_MIN
        self._pool_max = PG_POOL_MAX
        _init_metrics()

    @classmethod
    def get_instance(cls) -> PGConnectionManager:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @property
    def enabled(self) -> bool:
        return bool(PG_DSN)

    @property
    def is_connected(self) -> bool:
        return self._pg_available is True

    def _create_pool(self) -> bool:
        if not PG_DSN:
            self._pg_available = False
            logger.warning("PG pool: PG_DSN not set — billing in-memory")
            return False
        try:
            import psycopg2
            import psycopg2.pool

            dsn = PG_DSN
            if "connect_timeout" not in dsn:
                sep = "&" if "?" in dsn else "?"
                dsn = f"{dsn}{sep}connect_timeout={PG_POOL_TIMEOUT}"
            with self._pool_lock:
                if self._pool is not None:
                    try:
                        self._pool.closeall()
                    except Exception:
                        pass
                self._pool = psycopg2.pool.ThreadedConnectionPool(self._pool_min, self._pool_max, dsn)
            self._pg_available = True
            logger.info("PG pool created: min=%d max=%d", self._pool_min, self._pool_max)
            return True
        except Exception as e:
            self._pg_available = False
            logger.error("PG pool creation failed: %s", e)
            return False

    def _ensure_pool(self) -> bool:
        if not PG_DSN:
            return False
        if self._pool is not None and self._pg_available:
            return True
        return self._create_pool()

    def get_connection(self, operation: str = "query"):
        """Return _PgConnection context manager (retry + auto-commit)."""
        last_error = None
        for attempt in range(PG_RECONNECT_ATTEMPTS + 1):
            if not self._ensure_pool():
                break
            # Capture the lending pool: a concurrent _create_pool() may swap
            # self._pool, and the borrowed connection must go back to the pool
            # that handed it out.
            pool = self._pool
            conn = None
            try:
                conn = pool.getconn()
                cur = conn.cursor()
                try:
                    cur.execute("SELECT 1")
                except Exception:
                    # The probe failed: release the cursor before the connection
                    # is discarded, without masking the probe error.
                    try:
                        cur.close()
                    except Exception:
                        logger.warning("PG ping cursor close failed", exc_info=True)
                    raise
                cur.close()
                if _pg_conn_active:
                    _pg_conn_active.labels(pool="billing").inc()
                return _PgConnection(self, conn, pool)
            except Exception as e:
                if conn is not None:
                    # A failed ping must not leak the pool slot: close the
                    # borrowed connection instead of returning it as healthy.
                    try:
                        pool.putconn(conn, close=True)
                    except Exception:
                        logger.warning(
                            "PG putconn(close=True) failed after failed ping",
                            exc_info=True,
                        )
                        try:
                            conn.close()
                        except Exception:
                            logger.warning(
                                "PG connection close failed after failed ping",
                                exc_info=True,
                            )
                last_error = e
                self._error_count += 1
                if _pg_errors_total:
                    _pg_errors_total.labels(pool="billing", error_type=type(e).__name__).inc()
                if attempt == PG_RECONNECT_ATTEMPTS:
                    logger.error("PG exhausted after %d attempts: %s", PG_RECONNECT_ATTEMPTS, e)
                    break
                delay = PG_RETRY_BASE_DELAY * (2**attempt)
                logger.warning(
                    "PG retry %d/%d: %s — %.2fs",
                    attempt + 1,
                    PG_RECONNECT_ATTEMPTS,
                    e,
                    delay,
                )
                if _pg_reconnects_total:
                    _pg_reconnects_total.labels(pool="billing").inc()
                self._reconnect_count += 1
                self._pg_available = False
                time.sleep(delay)
        self._pg_available = False
        raise PGUnavailableError(f"PG unavailable: {last_error}")

    def health(self) -> dict:
        info = {
            "connected": self._pg_available is True,
            "pool_configured": bool(PG_DSN),
            "pool_size": f"{self._pool_min}/{self._pool_max}",
            "reconnect_count": self._reconnect_count,
            "error_count": self._error_count,
        }
        if not PG_DSN:
            info["status"] = "disabled"
            return info
        if self._pg_available is None:
            info["status"] = "uninitialized"
            return info
        if not self._pg_available:
            if not self._ensure_pool():
                info["status"] = "unavailable"
                return info
            info["connected"] = True
        try:
            with self.get_connection("health") as conn:
                cur = conn.cursor()
                cur.execute("SELECT version()")
                ver = cur.fetchone()[0]
                cur.close()
            info["status"] = "healthy"
            info["version"] = ver.split(",")[0]
        except PGUnavailableError:
            info["status"] = "unreachable"
            info["connected"] = False
        except Exception as e:
            info["status"] = "degraded"
            info["error"] = str(e)[:100]
        return info

    def shutdown(self):
        if self._pool:
            try:
                self._pool.closeall()
                logger.info("PG pool closed")
            except Exception as e:
                logger.warning("PG close err: %s", e)
        self._pool = None
        self._pg_available = None


_conn_mgr: Optional[PGConnectionManager] = None


def get_pg_manager() -> PGConnectionManager:
    global _conn_mgr
    if _conn_mgr is None:
        _conn_mgr = PGConnectionManager.get_instance()
    return _conn_mgr


def pg_health() -> dict:
    return get_pg_manager().health()


def shutdown_pg():
    global _conn_mgr
    if _conn_mgr:
        _conn_mgr.shutdown()
        _conn_mgr = None
