"""ROMA Billing Ledger — PostgreSQL-backed with connection pool, retry, in-memory fallback."""
from __future__ import annotations

import time
import json
import logging
from typing import Optional

from billing.pg_connection import get_pg_manager, PGUnavailableError

logger = logging.getLogger("roma.billing.ledger")


class PGBillingLedger:
    """Append-only billing ledger — PG-first with automatic in-memory fallback."""

    def __init__(self):
        self._pg = get_pg_manager()
        self._entries: list[dict] = []  # in-memory fallback

    def _pg_execute(self, operation: str, query: str, params: tuple = None,
                    fetch: bool = False) -> list | None:
        """Execute a PG query with automatic retry + fallback."""
        if not self._pg._ensure_pool():
            raise PGUnavailableError("PG not configured or unavailable")
        try:
            with self._pg.get_connection(operation) as conn:
                cur = conn.cursor()
                cur.execute(query, params or ())
                if fetch:
                    result = cur.fetchall()
                else:
                    result = None
                cur.close()
                return result
        except PGUnavailableError:
            raise
        except Exception as e:
            logger.error("BillingLedger.%s PG error: %s", operation, e)
            raise PGUnavailableError(str(e)) from e

    # ── Public API ───────────────────────────────────────────────

    def append(self, tenant_id: str, entry_type: str, amount: float,
               currency: str = "USD", metadata: dict = None) -> None:
        entry_type = entry_type.upper()
        meta_json = json.dumps(metadata or {})
        ledger_id = f"led-{int(time.time() * 1000)}-{hash(tenant_id + entry_type + str(amount)) & 0xFFFFF:05x}"
        try:
            self._pg_execute(
                "ledger_append",
                """INSERT INTO ledger_entries (ledger_id, tenant_id, entry_type, amount, currency, metadata)
                   VALUES (%s, %s, %s, %s, %s, %s::jsonb)""",
                (ledger_id, tenant_id, entry_type, amount, currency, meta_json),
            )
            return
        except PGUnavailableError:
            pass

        # In-memory fallback
        self._entries.append({
            "ledger_id": ledger_id, "timestamp": time.time(),
            "tenant_id": tenant_id, "type": entry_type,
            "amount": amount, "currency": currency,
            "metadata": metadata or {},
        })

    def credit(self, tenant_id: str, amount: float, currency: str = "USD", **meta) -> None:
        self.append(tenant_id, "CREDIT", amount, currency, meta)

    def debit(self, tenant_id: str, amount: float, currency: str = "USD", **meta) -> None:
        self.append(tenant_id, "DEBIT", amount, currency, meta)

    def get_tenant_balance(self, tenant_id: str) -> float:
        try:
            rows = self._pg_execute(
                "ledger_balance",
                """SELECT COALESCE(SUM(CASE WHEN entry_type='CREDIT' THEN amount ELSE -amount END), 0)
                   FROM ledger_entries WHERE tenant_id = %s""",
                (tenant_id,), fetch=True,
            )
            return float(rows[0][0])
        except PGUnavailableError:
            pass
        balance = 0.0
        for e in self._entries:
            if e["tenant_id"] == tenant_id:
                balance += e["amount"] if e["type"] == "CREDIT" else -e["amount"]
        return balance

    def get_balance(self, tenant_id: str) -> float:
        return self.get_tenant_balance(tenant_id)

    def get_tenant_entries(self, tenant_id: str, limit: int = 100) -> list[dict]:
        try:
            rows = self._pg_execute(
                "ledger_entries",
                """SELECT ledger_id, tenant_id, entry_type, amount, currency, metadata, created_at
                   FROM ledger_entries WHERE tenant_id = %s
                   ORDER BY created_at DESC LIMIT %s""",
                (tenant_id, limit), fetch=True,
            )
            return [
                {"ledger_id": r[0], "tenant_id": r[1], "type": r[2],
                 "amount": r[3], "currency": r[4], "metadata": r[5] or {},
                 "timestamp": r[6].timestamp()} for r in rows
            ]
        except PGUnavailableError:
            pass
        return [e for e in self._entries if e["tenant_id"] == tenant_id][-limit:]

    def ledger_summary(self) -> dict:
        try:
            rows = self._pg_execute(
                "ledger_summary",
                """SELECT tenant_id, entry_type, SUM(amount), COUNT(*)
                   FROM ledger_entries GROUP BY tenant_id, entry_type""",
                fetch=True,
            )
            by_tenant = {}
            for tenant_id, etype, total, cnt in rows:
                t = by_tenant.setdefault(tenant_id, {"credits": 0.0, "debits": 0.0, "net": 0.0, "entries": 0})
                t["entries"] += cnt
                if etype == "CREDIT":
                    t["credits"] += total
                else:
                    t["debits"] += total
            for t in by_tenant:
                by_tenant[t]["net"] = by_tenant[t]["credits"] - by_tenant[t]["debits"]
            return by_tenant
        except PGUnavailableError:
            pass
        by_tenant = {}
        for e in self._entries:
            t = by_tenant.setdefault(e["tenant_id"], {"credits": 0.0, "debits": 0.0, "net": 0.0, "entries": 0})
            t["entries"] += 1
            if e["type"] == "CREDIT":
                t["credits"] += e["amount"]
            else:
                t["debits"] += e["amount"]
        for t in by_tenant:
            by_tenant[t]["net"] = by_tenant[t]["credits"] - by_tenant[t]["debits"]
        return by_tenant

    @property
    def is_persistent(self) -> bool:
        return self._pg._create_pool() if self._pg._pool is None else self._pg.is_connected
