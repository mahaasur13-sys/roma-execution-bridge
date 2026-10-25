"""PA-02 (RaaS-04): admission spend-cap must fail closed.

- cap=0 must NOT admit paid work.
- an unknown balance (PG down / non-persistent ledger) must NOT admit.
- a balance read error must NOT admit.
- a real zero balance (durable ledger) IS distinguishable from unknown and may
  admit when the projected cost is within cap.
"""

import sys
import types

# Minimal pydantic_settings stub so `import main` works in an environment without
# the declared dependency (crypto_payments/settings.py imports BaseSettings).
if "pydantic_settings" not in sys.modules:
    _fake = types.ModuleType("pydantic_settings")

    class _BaseSettings:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    _fake.BaseSettings = _BaseSettings
    sys.modules["pydantic_settings"] = _fake

from billing.pg_ledger import PGUnavailableError
import main


class _FakeLedger:
    def __init__(self, persistent=True, balance=0.0, raises=False):
        self._persistent = persistent
        self._balance = balance
        self._raises = raises

    @property
    def is_persistent(self) -> bool:
        return self._persistent

    def get_tenant_balance(self, tenant_id):
        if self._raises:
            raise PGUnavailableError("pg down")
        return self._balance


def _setup(monkeypatch, cap, ledger):
    monkeypatch.setattr(
        main,
        "_plan_limits_or_refusal",
        lambda plan: ({"spend_cap_usd": cap, "max_jobs_per_month": -1}, None),
    )
    monkeypatch.setattr(main, "billing_ledger", ledger)
    monkeypatch.setattr(main, "track_spend_cap", lambda *a, **k: None)
    monkeypatch.setattr(main, "track_spend_cap_blocked", lambda *a, **k: None)
    monkeypatch.setattr(main, "alert_dispatcher", types.SimpleNamespace(send=lambda *a, **k: None))


def test_cap_zero_blocks_paid_work(monkeypatch):
    _setup(monkeypatch, cap=0.0, ledger=_FakeLedger())
    allowed, reason = main._check_spend_cap("t", 1.0, "plan")
    assert allowed is False
    assert "zero" in reason


def test_negative_cap_is_unlimited(monkeypatch):
    _setup(monkeypatch, cap=-1.0, ledger=_FakeLedger())
    allowed, reason = main._check_spend_cap("t", 1.0, "plan")
    assert allowed is True


def test_non_persistent_ledger_blocks(monkeypatch):
    _setup(monkeypatch, cap=5.0, ledger=_FakeLedger(persistent=False))
    allowed, reason = main._check_spend_cap("t", 1.0, "plan")
    assert allowed is False
    assert "unavailable" in reason


def test_balance_read_error_blocks(monkeypatch):
    _setup(monkeypatch, cap=5.0, ledger=_FakeLedger(persistent=True, raises=True))
    allowed, reason = main._check_spend_cap("t", 1.0, "plan")
    assert allowed is False


def test_real_zero_balance_admits_within_cap(monkeypatch):
    _setup(monkeypatch, cap=5.0, ledger=_FakeLedger(persistent=True, balance=0.0))
    allowed, reason = main._check_spend_cap("t", 1.0, "plan")
    assert allowed is True


def test_projected_over_cap_blocks(monkeypatch):
    _setup(monkeypatch, cap=5.0, ledger=_FakeLedger(persistent=True, balance=4.5))
    allowed, reason = main._check_spend_cap("t", 1.0, "plan")
    assert allowed is False
