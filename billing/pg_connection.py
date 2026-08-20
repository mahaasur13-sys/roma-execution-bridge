"""ROMA Billing — PostgreSQL Connection Manager (fixed)."""
from __future__ import annotations
import os, time, threading, logging
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
    """Class-based context manager: acquires conn, commits, returns to pool."""
    __slots__ = ("_mgr", "_conn", "_returned")
    def __init__(self, mgr, conn):
        self._mgr = mgr; self._conn = conn; self._returned = False
    def __enter__(self):
        return self._conn
    def __exit__(self, *a):
        if self._conn and not self._returned:
            try:
                self._conn.commit()
            except Exception:
                pass
            try:
                self._mgr._pool.putconn(self._conn)
                self._returned = True
            except Exception:
                try:
                    self._mgr._pool.putconn(self._conn, close=True)
                    self._returned = True
                except Exception:
                    pass
        if _pg_conn_active:
            _pg_conn_active.labels(pool="billing").dec()
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
            import psycopg2, psycopg2.pool
            dsn = PG_DSN
            if "connect_timeout" not in dsn:
                sep = "&" if "?" in dsn else "?"
                dsn = f"{dsn}{sep}connect_timeout={PG_POOL_TIMEOUT}"
            with self._pool_lock:
                if self._pool is not None:
                    try: self._pool.closeall()
                    except: pass
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
            try:
                conn = self._pool.getconn()
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.close()
                if _pg_conn_active:
                    _pg_conn_active.labels(pool="billing").inc()
                return _PgConnection(self, conn)
            except Exception as e:
                last_error = e
                self._error_count += 1
                if _pg_errors_total:
                    _pg_errors_total.labels(pool="billing", error_type=type(e).__name__).inc()
                if attempt == PG_RECONNECT_ATTEMPTS:
                    logger.error("PG exhausted after %d attempts: %s", PG_RECONNECT_ATTEMPTS, e)
                    break
                delay = PG_RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning("PG retry %d/%d: %s — %.2fs", attempt+1, PG_RECONNECT_ATTEMPTS, e, delay)
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
            info["status"] = "disabled"; return info
        if self._pg_available is None:
            info["status"] = "uninitialized"; return info
        if not self._pg_available:
            info["status"] = "unavailable"; return info
        try:
            with self.get_connection("health") as conn:
                cur = conn.cursor(); cur.execute("SELECT version()")
                ver = cur.fetchone()[0]; cur.close()
            info["status"] = "healthy"
            info["version"] = ver.split(",")[0]
        except PGUnavailableError:
            info["status"] = "unreachable"; info["connected"] = False
        except Exception as e:
            info["status"] = "degraded"; info["error"] = str(e)[:100]
        return info

    def shutdown(self):
        if self._pool:
            try: self._pool.closeall(); logger.info("PG pool closed")
            except Exception as e: logger.warning("PG close err: %s", e)
        self._pool = None; self._pg_available = None


_conn_mgr: Optional[PGConnectionManager] = None

def get_pg_manager() -> PGConnectionManager:
    global _conn_mgr
    if _conn_mgr is None:
        _conn_mgr = PGConnectionManager.get_instance()
    return _conn_mgr

def pg_health() -> dict: return get_pg_manager().health()
def shutdown_pg():
    global _conn_mgr
    if _conn_mgr: _conn_mgr.shutdown(); _conn_mgr = None
