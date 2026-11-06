"""OT-70 — debit_if_funds без idempotency_key даёт случайный ledger_id (не идемпотентно).

Фикс: без ключа (или job_id) — ValueError, а не случайный `led-<ts>-<uuid>`.
Проверка на верхнем уровне (до PG): тест не требует живого PG.
"""


import pytest


from billing.pg_ledger import PGBillingLedger  # noqa: E402


def test_debit_if_funds_requires_idempotency_key():
    ledger = PGBillingLedger()
    with pytest.raises(ValueError, match="idempotency_key"):
        ledger.debit_if_funds("tenant-ot70", 1.0)
