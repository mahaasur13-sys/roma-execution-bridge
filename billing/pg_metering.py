"""ROMA Metering Engine — PG-backed with connection pool, retry, in-memory fallback."""

from __future__ import annotations

import time
import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List

from billing.pg_connection import get_pg_manager, PGUnavailableError

logger = logging.getLogger("roma.billing.metering")

GPU_RATE = 0.00001
CPU_RATE = 0.000001
RAM_RATE = 0.0000001


@dataclass
class UsageEvent:
    tenant_id: str
    event_type: str
    value: float
    job_id: str
    timestamp: float = field(default_factory=time.time)
    cost_usd: float = 0.0

    def __post_init__(self):
        if self.event_type == "gpu_usage":
            self.cost_usd = self.value * GPU_RATE
        elif self.event_type == "cpu_usage":
            self.cost_usd = self.value * CPU_RATE
        elif self.event_type == "storage_usage":
            self.cost_usd = self.value * RAM_RATE
        elif self.event_type == "plugin_exec":
            self.cost_usd = self.value * 0.001
        elif self.event_type == "token_usage":
            self.cost_usd = self.value


class PGMeteringEngine:
    """Metering engine with PG primary + in-memory fallback."""

    def __init__(self):
        self._pg = get_pg_manager()
        self.events: List[UsageEvent] = []
        self.tenant_totals: Dict[str, Dict[str, float]] = {}

    def _pg_execute(
        self, operation: str, query: str, params: tuple = None, fetch: bool = False
    ) -> list | None:
        if not self._pg.enabled:
            raise PGUnavailableError("PG not configured or unavailable")
        # NB: do NOT pre-check is_connected — the pool is created lazily by
        # get_connection(); pre-checking it made the first record() raise before
        # ever attempting a connection (RaaS-01 durable-write path).
        try:
            with self._pg.get_connection(operation) as conn:
                cur = conn.cursor()
                cur.execute(query, params or ())
                result = cur.fetchall() if fetch else None
                cur.close()
                return result
        except PGUnavailableError:
            raise
        except Exception as e:
            logger.error("MeteringEngine.%s PG error: %s", operation, e)
            raise PGUnavailableError(str(e)) from e

    def record(
        self,
        event_type,
        tenant,
        job_id="manual",
        gpu_seconds=0,
        cpu_seconds=0,
        gb_seconds=0,
        plugin_count=0,
        billed=False,
        cost_usd=None,
        value=None,
    ) -> UsageEvent:
        val = (
            value
            if value is not None
            else (gpu_seconds or cpu_seconds or gb_seconds or float(plugin_count))
        )
        ev = UsageEvent(
            tenant_id=tenant, event_type=event_type, value=val, job_id=job_id
        )
        # F1: пишем авторитетную (уже округлённую) цену, чтобы usage_events.cost_usd
        # совпадал с DEBIT в лэджере (никаких 0.0012003365080902586…).
        ev.cost_usd = (
            round(float(cost_usd), 8) if cost_usd is not None else round(ev.cost_usd, 8)
        )

        try:
            _usage_insert_sql = """INSERT INTO usage_events (tenant_id, event_type, value, cost_usd, job_id, metadata, billed)
                   VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)"""
            if job_id and job_id not in ("manual", "auto"):
                # RaaS-01 / migration 016: idempotent write for a REAL job_id.
                # A repeated write is a no-op — it does NOT update cost_usd or
                # billed of the already-lying row.
                _usage_insert_sql += (
                    " ON CONFLICT (tenant_id, job_id, event_type)"
                    " WHERE job_id IS NOT NULL AND job_id NOT IN ('manual', 'auto', '')"
                    " DO NOTHING"
                )
            self._pg_execute(
                "usage_insert",
                _usage_insert_sql,
                (
                    tenant,
                    event_type,
                    val,
                    ev.cost_usd,
                    job_id,
                    json.dumps({}),
                    bool(billed),
                ),
            )
            self._pg_execute(
                "usage_totals_upsert",
                """INSERT INTO tenant_usage_totals (tenant_id, gpu_seconds, cpu_seconds, gb_seconds, total_cost, jobs_completed)
                   VALUES (%s, %s, %s, %s, %s, 0)
                   ON CONFLICT (tenant_id) DO UPDATE SET
                     gpu_seconds = tenant_usage_totals.gpu_seconds + EXCLUDED.gpu_seconds,
                     cpu_seconds = tenant_usage_totals.cpu_seconds + EXCLUDED.cpu_seconds,
                     gb_seconds  = tenant_usage_totals.gb_seconds  + EXCLUDED.gb_seconds,
                     total_cost  = tenant_usage_totals.total_cost  + EXCLUDED.total_cost,
                     updated_at  = NOW()""",
                (
                    tenant,
                    gpu_seconds if event_type == "gpu_usage" else 0,
                    cpu_seconds if event_type == "cpu_usage" else 0,
                    gb_seconds if event_type == "storage_usage" else 0,
                    ev.cost_usd,
                ),
            )
            return ev
        except PGUnavailableError as exc:
            # RaaS-01: a failed durable write must not be silently swallowed, and
            # the in-memory fallback must not report false success for paid work.
            # Make the failure visible and correlated by tenant/job so upstream
            # (execution_worker) can fail closed on PGUnavailableError.
            logger.error(
                "MeteringEngine.record NOT persisted (fail-closed): "
                "tenant=%s job=%s event=%s error=%s",
                tenant,
                job_id,
                event_type,
                exc,
            )
            raise

    def snapshot(self, tenant: str = None) -> Dict:
        try:
            if tenant:
                rows = self._pg_execute(
                    "totals_one",
                    """SELECT gpu_seconds, cpu_seconds, gb_seconds, total_cost, jobs_completed
                       FROM tenant_usage_totals WHERE tenant_id = %s""",
                    (tenant,),
                    fetch=True,
                )
                if rows:
                    return {
                        "gpu_s": rows[0][0],
                        "cpu_s": rows[0][1],
                        "gb_s": rows[0][2],
                        "cost": rows[0][3],
                        "jobs": rows[0][4],
                    }
                return {}
            rows = self._pg_execute(
                "totals_all",
                "SELECT tenant_id, gpu_seconds, cpu_seconds, gb_seconds, total_cost, jobs_completed FROM tenant_usage_totals",
                fetch=True,
            )
            cnt_rows = self._pg_execute(
                "count_events", "SELECT COUNT(*) FROM usage_events", fetch=True
            )
            total_events = cnt_rows[0][0] if cnt_rows else 0
            tenants = {
                r[0]: {
                    "gpu_s": r[1],
                    "cpu_s": r[2],
                    "gb_s": r[3],
                    "cost": r[4],
                    "jobs": r[5],
                }
                for r in rows
            }
            return {
                "tenants": tenants,
                "total_events": total_events,
                "total_cost": sum(t["cost"] for t in tenants.values()),
            }
        except PGUnavailableError:
            pass
        if tenant:
            return self.tenant_totals.get(tenant, {})
        return {
            "tenants": self.tenant_totals,
            "total_events": len(self.events),
            "total_cost": sum(t["cost"] for t in self.tenant_totals.values()),
        }

    @property
    def is_persistent(self) -> bool:
        return self._pg.enabled and self._pg.is_connected
