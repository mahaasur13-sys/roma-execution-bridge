"""B3: legacy billing.ledger is deprecated; package re-export points to PGBillingLedger."""

import pytest


def test_billing_package_reexports_pg_ledger():
    from billing import BillingLedger
    from billing.pg_ledger import PGBillingLedger

    # `from billing import BillingLedger` now yields the active PG-first ledger,
    # not the legacy in-memory one.
    assert BillingLedger is PGBillingLedger


def test_legacy_ledger_is_deprecated():
    from billing.ledger import BillingLedger

    with pytest.warns(DeprecationWarning):
        BillingLedger()
