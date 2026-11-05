"""OT-98 (S9): при конфликте ledger_id сверяются тип/сумма/валюта/job.

На базе ``ledger_idem_lookup`` возвращал существующий ``ledger_id`` без сверки,
т.е. чужую запись (CREDIT/другая сумма/валюта/job) можно было выдать за свой дебет.
Фикс: fail-closed — сверка полей, при несовпадении лог и None. Только живой PG.
"""

import hashlib
import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from billing.pg_ledger import PGBillingLedger  # noqa: E402

pytestmark = pytest.mark.pg  # G-CI-PG-CANON: env-skip без живого PG


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _seed_row(ledger, ledger_id, tenant, entry_type, amount, currency, job_id):
    ledger._pg_execute(
        "s9_seed",
        """INSERT INTO ledger_entries (ledger_id, tenant_id, entry_type, amount, currency, metadata)
           VALUES (%s, %s, %s, %s, %s, %s::jsonb)
           ON CONFLICT (ledger_id) DO NOTHING""",
        (ledger_id, tenant, entry_type, amount, currency, json.dumps({"job_id": job_id})),
    )


def test_s9_idem_lookup_verifies_fields():
    ledger = PGBillingLedger()
    tenant = _uniq("test-s9")
    key = f"job:{_uniq('s9')}"
    amount = 1.0
    ledger.credit(tenant, 100.0)

    ledger_id = "led-" + hashlib.sha256(f"{tenant}:{key}".encode()).hexdigest()[:24]

    # Несовпадение: CREDIT, другая сумма/валюта/job → fail-closed (None), не чужой id.
    _seed_row(ledger, ledger_id, tenant, "CREDIT", 999.0, "EUR", "other")
    rid = ledger.debit_if_funds(tenant, amount, idempotency_key=key, job_id="expected")
    assert rid is None, "несовпадение полей не должно возвращать существующий ledger_id"

    # Совпадение: DEBIT с теми же суммой/валютой/job → возврат ledger_id.
    tenant2 = _uniq("test-s9")
    key2 = f"job:{_uniq('s9')}"
    ledger.credit(tenant2, 100.0)
    lid2 = "led-" + hashlib.sha256(f"{tenant2}:{key2}".encode()).hexdigest()[:24]
    _seed_row(ledger, lid2, tenant2, "DEBIT", amount, "USD", "expected")
    rid2 = ledger.debit_if_funds(tenant2, amount, idempotency_key=key2, job_id="expected")
    assert rid2 == lid2, "совпадающая запись должна вернуть ledger_id"
