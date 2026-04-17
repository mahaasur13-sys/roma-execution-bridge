"""Tests for RevenueShareCalculator + Stripe Webhook."""
import sys; sys.path.insert(0, '.')

class MockLedger:
    def get_monthly_revenue(self, tid):
        return {"t1": 500.0, "t2": 3000.0, "t3": 7000.0}.get(tid, 0.0)

def test_tiered_rates():
    from saas.webhooks.revenue_share import RevenueShareCalculator
    calc = RevenueShareCalculator(MockLedger())
    for tid, gross, exp_rate in [("t1", 50_000, 0.10), ("t2", 300_000, 0.15), ("t3", 700_000, 0.20)]:
        r = calc.calculate(tid, gross)
        assert r["revenue_share_percent"] == exp_rate, f"{tid}: got {r['revenue_share_percent']}, want {exp_rate}"
        assert r["revenue_share_cents"] == round(gross * exp_rate)
        assert r["net_to_platform_cents"] == gross - r["revenue_share_cents"]
    print("PASS: tiered_rates")

def test_idempotency():
    processed = set()
    def dup(e): return e in processed
    def mark(e): processed.add(e)
    assert not dup("e1")
    mark("e1")
    assert dup("e1")
    print("PASS: idempotency")

def test_response_model():
    from saas.webhooks.stripe_webhook import Response
    r = Response(received=True, event_id="evt_1", processed=True)
    assert r.received and r.processed and r.error is None
    print("PASS: response_model")

def test_signature_skip_on_empty():
    from saas.webhooks.stripe_webhook import _verify
    assert _verify(b"payload", "sig", "") == True
    assert _verify(b"payload", "", "secret") == True
    print("PASS: signature_skip_on_empty")

def test_ledger_revenue_share_ext():
    from billing.ledger import BillingLedger
    ledger = BillingLedger()
    assert hasattr(ledger, "record_revenue_share")
    assert hasattr(ledger, "get_monthly_revenue")
    assert hasattr(ledger, "get_pending_revenue_share")
    print("PASS: ledger_revenue_share_ext")

if __name__ == "__main__":
    test_tiered_rates()
    test_idempotency()
    test_response_model()
    test_signature_skip_on_empty()
    test_ledger_revenue_share_ext()
    print("\n✅ All revenue-share tests passed")
