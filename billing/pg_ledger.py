"""ROMA Billing Ledger — PostgreSQL-backed with connection pool and retry; money writes and balance are fail-closed."""

from __future__ import annotations

import time
import json
import logging
import math
import os
import hashlib
import zlib
from uuid import uuid4

from billing.pg_connection import get_pg_manager, PGUnavailableError
from billing.spend_caps import MONEY_POLICY
from cost.money_policy import reject_invalid_money_amount, reject_nullable_money

logger = logging.getLogger("roma.billing.ledger")

_DEBIT_IF_FUNDS_INSERT_SQL = """
    WITH bal AS (
        SELECT COALESCE(SUM(CASE WHEN entry_type = 'CREDIT' THEN amount
                                 ELSE -amount END), 0) AS b
          FROM ledger_entries
         WHERE tenant_id = %s
    )
    INSERT INTO ledger_entries (ledger_id, tenant_id, entry_type, amount, currency, metadata)
    SELECT %s, %s, 'DEBIT', %s, %s, %s::jsonb
      FROM bal
     WHERE bal.b >= %s
    ON CONFLICT (ledger_id) DO NOTHING
    RETURNING ledger_id
"""

_LEDGER_SUMMARY_SQL = """
    WITH per_tenant AS (
        SELECT tenant_id,
               COALESCE(SUM(CASE WHEN entry_type = 'DEBIT' THEN amount END), 0) AS debit,
               COUNT(*) AS entries
          FROM ledger_entries
         GROUP BY tenant_id
    ), usage AS (
        SELECT tenant_id, COALESCE(SUM(cost_usd), 0) AS cost
          FROM usage_events
         GROUP BY tenant_id
    ), diff AS (
        SELECT COALESCE(l.tenant_id, u.tenant_id) AS tenant_id,
               COALESCE(l.debit, 0) AS debit,
               ABS(COALESCE(l.debit, 0) - COALESCE(u.cost, 0)) AS d
          FROM per_tenant l
          FULL OUTER JOIN usage u ON u.tenant_id = l.tenant_id
    )
    SELECT
        (SELECT COALESCE(MAX(d), 0) FROM diff),
        (SELECT COUNT(*) FROM diff WHERE d > %s),
        (SELECT COALESCE(SUM(entries), 0) FROM per_tenant),
        (SELECT COALESCE(json_agg(json_build_object('tenant_id', tenant_id, 'diff', d) ORDER BY d DESC),
                         '[]'::json)
           FROM (SELECT tenant_id, d FROM diff WHERE d > %s ORDER BY d DESC LIMIT 20) top),
        (SELECT COUNT(*)
           FROM diff
           JOIN tenants tn ON tn.id = diff.tenant_id
          WHERE COALESCE((%s::jsonb ->> tn.plan)::float8, 0) > 0
            AND diff.debit >= (%s::jsonb ->> tn.plan)::float8)
"""

DRIFT_THRESHOLD_DEFAULT = 0.05


def drift_threshold_from_env() -> float:
    """ROMA_LEDGER_DRIFT_THRESHOLD_USD — порог сверки; мусор/отрицательное → дефолт + warning."""
    raw = os.environ.get("ROMA_LEDGER_DRIFT_THRESHOLD_USD")
    if raw is None or not raw.strip():
        return DRIFT_THRESHOLD_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        logger.warning("ROMA_LEDGER_DRIFT_THRESHOLD_USD=%r не число — беру %.2f", raw, DRIFT_THRESHOLD_DEFAULT)
        return DRIFT_THRESHOLD_DEFAULT
    if not math.isfinite(value) or value < 0:
        logger.warning("ROMA_LEDGER_DRIFT_THRESHOLD_USD=%r вне диапазона — беру %.2f", raw, DRIFT_THRESHOLD_DEFAULT)
        return DRIFT_THRESHOLD_DEFAULT
    return value


class LedgerIdemMismatchError(Exception):
    """R2: по ключу идемпотентности уже есть запись, но с другими типом/суммой/валютой/job_id.

    Это НЕ «нет средств» (не 402) и НЕ двойной дебет — это конфликт ключа, который
    вызывающий код обязан отличить от no_funds и вернуть отдельную ошибку (409/500).
    """


class PGBillingLedger:
    """Append-only billing ledger — PG-first with automatic in-memory fallback."""

    def __init__(self):
        self._pg = get_pg_manager()
        self._entries: list[dict] = []  # in-memory fallback

    def _pg_execute(self, operation: str, query: str, params: tuple = None, fetch: bool = False) -> list | None:
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

    def append(
        self,
        tenant_id: str,
        entry_type: str,
        amount: float,
        currency: str = "USD",
        metadata: dict = None,
    ) -> str:
        """Record an entry and return its ``ledger_id`` (NF-1) — PG-first, fail-closed.

        The PG row is written first; only after a successful write is the entry
        mirrored into the in-memory list. When PG cannot persist the entry,
        ``PGUnavailableError`` is raised and nothing is kept in memory — a money
        entry that exists only in one worker's memory is lost on restart and
        invisible to other workers (same contract as ``credit()``).
        """
        # NOT NULL-договор (MONEY_WHITELIST_POLICY): amount/currency — обязательные
        # money-колонки; None отклоняется до INSERT с именем таблицы/колонки.
        reject_nullable_money("ledger_entries", "amount", amount)
        reject_nullable_money("ledger_entries", "currency", currency)
        # NaN/Inf/отрицательная сумма — отказ до INSERT (направление задаёт entry_type).
        reject_invalid_money_amount("ledger_entries", "amount", amount)
        entry_type = entry_type.upper()
        meta_json = json.dumps(metadata or {})
        # uuid4, not hash(): the built-in hash() is salted per process
        # (PYTHONHASHSEED), so the same entry got a different ledger_id in every
        # worker and the unique-key contract was not reproducible. One id is used
        # for both the PG row and the in-memory mirror.
        ledger_id = f"led-{int(time.time() * 1000)}-{uuid4().hex[:12]}"
        # Raises PGUnavailableError (fail-closed); _pg_execute already logs the
        # underlying error under operation="ledger_append".
        self._pg_execute(
            "ledger_append",
            """INSERT INTO ledger_entries (ledger_id, tenant_id, entry_type, amount, currency, metadata)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb)""",
            (ledger_id, tenant_id, entry_type, amount, currency, meta_json),
        )
        self._entries.append(
            {
                "ledger_id": ledger_id,
                "timestamp": time.time(),
                "tenant_id": tenant_id,
                "type": entry_type,
                "amount": amount,
                "currency": currency,
                "metadata": metadata or {},
            }
        )
        return ledger_id

    def credit(
        self, tenant_id: str, amount: float, currency: str = "USD", idempotency_key: str | None = None, **meta
    ) -> str:
        """Credit a tenant — PG-first, fail-closed, optionally idempotent.

        When ``idempotency_key`` is supplied the ``ledger_id`` is deterministic
        (``sha256(tenant:key)[:24]``) and the insert uses ``ON CONFLICT
        (ledger_id) DO NOTHING``, so a retry after a timed-out commit returns
        the SAME id instead of double-crediting. Raises PGUnavailableError when
        PG cannot persist the credit.
        """
        reject_nullable_money("ledger_entries", "amount", amount)
        reject_nullable_money("ledger_entries", "currency", currency)
        reject_invalid_money_amount("ledger_entries", "amount", amount)
        meta = dict(meta or {})
        if idempotency_key:
            digest = hashlib.sha256(f"{tenant_id}:{idempotency_key}".encode()).hexdigest()
            ledger_id = f"led-{digest[:24]}"
            meta["idempotency_key"] = idempotency_key
        else:
            ledger_id = f"led-{int(time.time() * 1000)}-{uuid4().hex[:12]}"
        meta_json = json.dumps(meta)
        rows = self._pg_execute(
            "ledger_credit",
            """INSERT INTO ledger_entries (ledger_id, tenant_id, entry_type, amount, currency, metadata)
               VALUES (%s, %s, 'CREDIT', %s, %s, %s::jsonb)
               ON CONFLICT (ledger_id) DO NOTHING
               RETURNING ledger_id""",
            (ledger_id, tenant_id, amount, currency, meta_json),
            fetch=True,
        )
        if rows:
            self._entries.append(
                {
                    "ledger_id": ledger_id,
                    "timestamp": time.time(),
                    "tenant_id": tenant_id,
                    "type": "CREDIT",
                    "amount": amount,
                    "currency": currency,
                    "metadata": meta,
                }
            )
            return rows[0][0]
        # ON CONFLICT: already credited under this idempotency key → return existing id.
        if idempotency_key:
            existing = self._pg_execute(
                "ledger_credit_idem_lookup",
                "SELECT ledger_id FROM ledger_entries WHERE ledger_id = %s AND tenant_id = %s",
                (ledger_id, tenant_id),
                fetch=True,
            )
            if existing:
                return existing[0][0]
        # A fresh uuid4 id cannot collide; fail-closed rather than double-credit.
        raise PGUnavailableError("ledger credit conflict")

    def refund(self, tenant_id: str, amount: float, currency: str = "USD", job_id: str | None = None, reason: str = "refund") -> str:
        """C10: вернуть дебет (CREDIT-запись), идемпотентно по refund:<job_id>.

        Возврат без запроса клиента (отменённая/недоставленная работа). Повторный
        вызов с тем же job_id не удваивает возврат.
        """
        if not job_id:
            raise ValueError("refund requires job_id (idempotency key)")
        return self.credit(
            tenant_id,
            amount,
            currency=currency,
            idempotency_key=f"refund:{job_id}",
            refund_for=job_id,
            reason=reason,
        )

    def debit(self, tenant_id: str, amount: float, currency: str = "USD", **meta) -> None:
        """Unconditional DEBIT via ``append()``: raises PGUnavailableError when PG is down."""
        self.append(tenant_id, "DEBIT", amount, currency, meta)

    def _txn(self, operation, statements, fetch_last=True):
        """Path A: lock+INSERT в одной транзакции. Без autocommit.
        ping уже открыл INTRANS → BEGIN no-op; COMMIT/ROLLBACK явные;
        __exit__ CM коммитит вхолостую (no-op на IDLE)."""
        if not self._pg._ensure_pool():
            raise PGUnavailableError("PG not configured or unavailable")
        try:
            with self._pg.get_connection(operation) as conn:
                cur = conn.cursor()
                try:
                    cur.execute("BEGIN")
                    rows = None
                    for idx, (sql, params) in enumerate(statements):
                        cur.execute(sql, params)
                        if fetch_last and idx == len(statements) - 1:
                            rows = cur.fetchall()
                    cur.execute("COMMIT")
                    return rows
                except Exception:
                    try:
                        cur.execute("ROLLBACK")
                    except Exception:
                        pass
                    raise
                finally:
                    cur.close()
        except PGUnavailableError:
            raise
        except Exception as e:
            logger.error("BillingLedger.%s PG error: %s", operation, e)
            raise PGUnavailableError(str(e)) from e

    def debit_if_funds(self, tenant_id, amount, currency="USD", idempotency_key=None, **meta) -> tuple[str | None, bool]:
        if amount < 0:
            raise ValueError("debit amount must be >= 0")
        reject_invalid_money_amount("ledger_entries", "amount", amount)
        # OT-70: без идемпотентного ключа ledger_id был бы случайным — повторный
        # вызов после неопределённого COMMIT дал бы двойное списание.
        if not idempotency_key:
            raise ValueError("debit_if_funds requires idempotency_key (or job_id)")
        digest = hashlib.sha256(f"{tenant_id}:{idempotency_key}".encode()).hexdigest()
        ledger_id = f"led-{digest[:24]}"
        meta = dict(meta or {})
        meta["idempotency_key"] = idempotency_key
        meta_json = json.dumps(meta)
        lock_key = zlib.crc32(tenant_id.encode()) & 0x7FFFFFFF

        try:
            rows = self._txn(
                "ledger_debit_if_funds",
                [
                    ("SELECT pg_advisory_xact_lock(%s)", (lock_key,)),
                    (
                        _DEBIT_IF_FUNDS_INSERT_SQL,
                        (
                            tenant_id,
                            ledger_id,
                            tenant_id,
                            amount,
                            currency,
                            meta_json,
                            amount,
                        ),
                    ),
                ],
                fetch_last=True,
            )
        except PGUnavailableError:
            # Indeterminate COMMIT: the INSERT may have committed server-side.
            # Re-check by ledger_id (idempotent) before treating it as failed.
            if idempotency_key:
                rid = self._ledger_idem_lookup(ledger_id, tenant_id, amount, currency, meta)
                if rid:
                    # R1: если запись нашлась — её закоммитил ЭТОТ вызов (INSERT прошёл,
                    # а не был no-op по существующему ключу), значит это НОВЫЙ дебет:
                    # usage/метрики обязаны писаться, иначе ledger расходится с usage.
                    return rid, True
            raise

        if rows:
            rid = rows[0][0]
            self._entries.append(
                {
                    "ledger_id": rid,
                    "timestamp": time.time(),
                    "tenant_id": tenant_id,
                    "type": "DEBIT",
                    "amount": amount,
                    "currency": currency,
                    "metadata": meta,
                }
            )
            return rid, True
        if idempotency_key:
            return self._ledger_idem_lookup(ledger_id, tenant_id, amount, currency, meta), False
        return None, False

    def _ledger_idem_lookup(self, ledger_id, tenant_id, expected_amount, currency, meta) -> str | None:
        """OT-98: сверка существующей записи перед возвратом успеха при конфликте.

        Возвращает ledger_id только если запись по (ledger_id, tenant_id) реально
        совпадает с ожидаемым дебетом (тип/сумма/валюта/job_id). Иначе — лог и None
        (fail-closed: не выдаём чужую запись за свой дебет).
        """
        rows = self._pg_execute(
            "ledger_idem_lookup",
            "SELECT entry_type, amount, currency, metadata FROM ledger_entries "
            "WHERE ledger_id = %s AND tenant_id = %s",
            (ledger_id, tenant_id),
            fetch=True,
        )
        if not rows:
            return None
        entry_type, existing_amount, existing_currency, existing_meta = rows[0]
        try:
            existing_meta = json.loads(existing_meta) if isinstance(existing_meta, str) else (existing_meta or {})
        except Exception:
            existing_meta = {}
        expected_job_id = (meta or {}).get("job_id")
        matches = (
            entry_type == "DEBIT"
            and round(float(existing_amount), 8) == round(float(expected_amount), 8)
            and existing_currency == currency
            and existing_meta.get("job_id") == expected_job_id
        )
        if matches:
            return ledger_id
        logger.warning(
            "ledger_idem_mismatch ledger_id=%s tenant=%s "
            "expected=(DEBIT,%s,%s,job=%s) got=(%s,%s,%s,job=%s)",
            ledger_id,
            tenant_id,
            expected_amount,
            currency,
            expected_job_id,
            entry_type,
            existing_amount,
            existing_currency,
            existing_meta.get("job_id"),
        )
        # R2: несовпадение — отдельная ошибка, НЕ «нет средств» (иначе ложный 402).
        raise LedgerIdemMismatchError(
            f"ledger_idem_mismatch ledger_id={ledger_id} tenant={tenant_id} "
            f"expected_amount={expected_amount} currency={currency} job_id={expected_job_id}"
        )

    def get_tenant_debit_sum(self, tenant_id: str) -> float:
        try:
            rows = self._pg_execute(
                "ledger_debit_sum",
                "SELECT COALESCE(SUM(amount), 0) FROM ledger_entries WHERE tenant_id = %s AND entry_type = 'DEBIT'",
                (tenant_id,),
                fetch=True,
            )
            return float(rows[0][0])
        except PGUnavailableError:
            pass
        return sum(e["amount"] for e in self._entries if e["tenant_id"] == tenant_id and e["type"] == "DEBIT")

    def get_tenant_usage_cost(self, tenant_id: str) -> float:
        try:
            rows = self._pg_execute(
                "usage_cost_sum",
                "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_events WHERE tenant_id = %s",
                (tenant_id,),
                fetch=True,
            )
            return float(rows[0][0])
        except PGUnavailableError:
            pass
        return 0.0

    def get_tenant_balance(self, tenant_id: str) -> float:
        """Balance from PG only — raises PGUnavailableError when PG is down (fail-closed).

        The in-memory mirror is per-process and incomplete (it never sees entries
        written by other workers), so a balance computed from it is a wrong number,
        not a degraded one; callers must refuse (admission → deny, routes → 503).
        """
        rows = self._pg_execute(
            "ledger_balance",
            """SELECT COALESCE(SUM(CASE WHEN entry_type='CREDIT' THEN amount ELSE -amount END), 0)
               FROM ledger_entries WHERE tenant_id = %s""",
            (tenant_id,),
            fetch=True,
        )
        return float(rows[0][0])

    def get_balance(self, tenant_id: str) -> float:
        return self.get_tenant_balance(tenant_id)

    def get_tenant_entries(self, tenant_id: str, limit: int = 100) -> list[dict]:
        try:
            rows = self._pg_execute(
                "ledger_entries",
                """SELECT ledger_id, tenant_id, entry_type, amount, currency, metadata, created_at
                   FROM ledger_entries WHERE tenant_id = %s
                   ORDER BY created_at DESC LIMIT %s""",
                (tenant_id, limit),
                fetch=True,
            )
            return [
                {
                    "ledger_id": r[0],
                    "tenant_id": r[1],
                    "type": r[2],
                    "amount": r[3],
                    "currency": r[4],
                    "metadata": r[5] or {},
                    "timestamp": r[6].timestamp(),
                }
                for r in rows
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
            t = by_tenant.setdefault(
                e["tenant_id"],
                {"credits": 0.0, "debits": 0.0, "net": 0.0, "entries": 0},
            )
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

    def get_tenant_entry_count(self, tenant_id: str) -> int:
        try:
            rows = self._pg_execute(
                "ledger_count",
                "SELECT COUNT(*) FROM ledger_entries WHERE tenant_id = %s",
                (tenant_id,),
                fetch=True,
            )
            return int(rows[0][0])
        except PGUnavailableError:
            pass
        return len([e for e in self._entries if e["tenant_id"] == tenant_id])

    def reconciliation_summary(self, threshold_usd: float | None = None) -> dict:
        """Сверка ledger↔usage по ВСЕМ тенантам одним запросом: агрегаты, без метки на тенанта.

        diff = |SUM(DEBIT) - SUM(usage cost)| по каждому тенанту; drifted — те, у кого
        diff > threshold_usd; drifted_ids — top-20 по |diff| (для лога, не для метрик).
        spend_cap_exceeded — тенанты, у кого списано >= spend-cap своего тарифа
        (тариф и числа — из `billing.spend_caps`, тот же источник, что у `deps.plan_config`).
        """
        threshold = DRIFT_THRESHOLD_DEFAULT if threshold_usd is None else float(threshold_usd)
        caps = json.dumps({plan: float(cfg["spend_cap_usd"]) for plan, cfg in MONEY_POLICY.items()})
        rows = self._pg_execute(
            "ledger_reconcile_summary",
            _LEDGER_SUMMARY_SQL,
            (threshold, threshold, caps, caps),
            fetch=True,
        )
        if not rows:
            return {
                "diff_max": 0.0,
                "drifted": 0,
                "entries_total": 0,
                "drifted_top": [],
                "spend_cap_exceeded": 0,
                "threshold_usd": threshold,
            }
        diff_max, drifted, entries, top, exceeded = rows[0]
        return {
            "diff_max": float(diff_max or 0.0),
            "drifted": int(drifted or 0),
            "entries_total": int(entries or 0),
            "drifted_top": list(top or []),
            "spend_cap_exceeded": int(exceeded or 0),
            "threshold_usd": threshold,
        }
