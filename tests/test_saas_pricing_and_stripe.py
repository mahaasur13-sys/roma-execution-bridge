"""N-BLACK-B: поведенческие тесты SaaS-прайсинга и Stripe-контура.

Зачем: saas/pricing_engine.py и saas/stripe_integration.py не покрывались.
Проверяется: ступени load-множителя, бесплатный тир как квота без списания, влияние тира и региона
на цену, маржа и markup, а также клиентский контур Stripe — клиенты, подписки, usage, счета,
оплата (в том числе повторная и чужая организация), оценка стоимости джобы и события вебхуков.
"""

import time

import pytest

from saas.pricing_engine import (
    GPU_COST,
    GPU_RATES,
    TIER_MONTHLY,
    TIER_QUOTA,
    PricingEngine,
    PricingTier,
    ProfitCalculator,
    load_mult,
)
from saas.stripe_integration import (
    SubscriptionStatus,
    StripeIntegration,
    WebhookSimulator,
)


def test_load_multiplier_bands():
    assert load_mult(0.0) == 0.8
    assert load_mult(0.29) == 0.8
    assert load_mult(0.3) == 1.0
    assert load_mult(0.69) == 1.0
    assert load_mult(0.7) == 1.5
    assert load_mult(0.89) == 1.5
    assert load_mult(0.9) == 2.0
    assert load_mult(1.0) == 2.0


def test_free_tier_quote_is_quota_only():
    quote = PricingEngine().quote(3600, "RTX4090", "free")

    assert quote.total_cost == 0.0
    assert quote.base_cost == 0.0
    assert quote.breakdown["quota_free"] is True
    assert quote.breakdown["monthly_quota_hrs"] == TIER_QUOTA[PricingTier.FREE] // 3600
    assert quote.estimated_hours == pytest.approx(1.0)
    assert quote.currency == "USD"


def test_paid_quote_applies_tier_region_and_load():
    quote = PricingEngine().quote(3600, "A100", "pro", region_mult=1.2, cluster_load=0.85)

    expected_rate = GPU_RATES["A100"] * 1.0 * 1.5 * 1.2
    assert quote.load_mult == 1.5
    assert quote.region_mult == 1.2
    assert quote.base_cost == pytest.approx(3600 * GPU_RATES["A100"])
    assert quote.total_cost == pytest.approx(3600 * expected_rate)
    assert quote.breakdown["tier_mult"] == 1.0
    assert quote.breakdown["effective_rate"] == pytest.approx(expected_rate)
    assert quote.breakdown["gpu_per_hour"] == pytest.approx(expected_rate * 3600)
    assert quote.breakdown["tier_monthly"] == TIER_MONTHLY[PricingTier.PRO]


def test_enterprise_tier_and_unknown_model_fallback():
    enterprise = PricingEngine().quote(3600, "H100", "enterprise")

    assert enterprise.breakdown["tier_mult"] == 2.5
    assert enterprise.total_cost == pytest.approx(3600 * GPU_RATES["H100"] * 2.5)

    fallback = PricingEngine().quote(3600, "GTX1080", "pro")
    assert fallback.breakdown["gpu_rate"] == GPU_RATES["RTX4090"]


def test_profit_calculator_margin_math():
    engine = PricingEngine()
    calculator = ProfitCalculator()
    quote = engine.quote(3600, "A100", "pro")

    margin = calculator.margin(quote)

    revenue = 3600 * GPU_RATES["A100"]
    cost = 3600 * GPU_COST["A100"]
    assert margin["revenue"] == pytest.approx(round(revenue, 4))
    assert margin["cost"] == pytest.approx(round(cost, 4))
    assert margin["profit"] == pytest.approx(round(revenue - cost, 4))
    assert margin["GPM"] == pytest.approx(round((revenue - cost) / revenue * 100, 1))
    assert margin["markup"] == pytest.approx(round(revenue / cost, 2))

    free_quote = engine.quote(3600, "A100", "free")
    free_margin = calculator.margin(free_quote)
    assert free_margin["revenue"] == 0.0
    assert free_margin["GPM"] == 0
    assert free_margin["markup"] == 0


def test_stripe_customer_and_subscription_lifecycle():
    stripe = StripeIntegration()

    customer = stripe.create_customer("org_acme", "billing@acme.com")
    assert customer.id.startswith("cus_")
    assert customer.stripe_customer_id.startswith("cus_stripe_")
    assert customer.email == "billing@acme.com"
    assert stripe.get_balance("org_acme") == 0

    subscription = stripe.create_subscription("org_acme", "pro")
    assert subscription.status is SubscriptionStatus.ACTIVE
    assert subscription.current_period_end - subscription.current_period_start == pytest.approx(
        30 * 24 * 3600
    )
    assert stripe.get_subscription("org_acme") is subscription

    assert stripe.cancel_subscription("org_acme") is True
    assert subscription.status is SubscriptionStatus.CANCELLED
    assert stripe.cancel_subscription("org_missing") is False
    assert stripe.get_subscription("org_missing") is None


def test_stripe_usage_summary_and_invoice_payment():
    stripe = StripeIntegration()
    customer = stripe.create_customer("org_acme", "billing@acme.com")

    stripe.record_usage("org_acme", customer.id[:12], 3600.0, 1.998, "job-1", "ml_training")
    stripe.record_usage("org_acme", customer.id[:12], 1800.0, 0.999, "job-1", "inference")
    stripe.record_usage("org_acme", customer.id[:12], 600.0, 0.3, "", "")
    stripe.record_usage("org_other", customer.id[:12], 10.0, 0.01, "job-9")

    summary = stripe.get_usage_summary("org_acme")
    assert summary["total_gpu_seconds"] == pytest.approx(6000.0)
    assert summary["total_cost"] == pytest.approx(3.297)
    assert summary["record_count"] == 3
    assert summary["jobs"] == 1
    assert len(stripe.get_usage("org_other")) == 1
    assert stripe.get_balance("org_acme") == pytest.approx(3.297)

    invoice = stripe.generate_invoice("org_acme")
    assert invoice.status == "open"
    assert invoice.total == pytest.approx(3.297)
    assert len(invoice.items) == 3
    assert invoice.items[0]["desc"] == "ml_training"
    assert invoice.items[2]["desc"] == "GPU compute"

    assert stripe.pay_invoice(invoice.id, "org_acme") is True
    assert invoice.status == "paid"
    assert invoice.paid_at > 0
    assert stripe.pay_invoice(invoice.id, "org_acme") is False
    assert stripe.pay_invoice(invoice.id, "org_other") is False


def test_stripe_job_cost_estimation_by_model_and_tier():
    stripe = StripeIntegration()

    assert stripe.estimate_job_cost(1000.0, "A100", "pro") == pytest.approx(0.555)
    assert stripe.estimate_job_cost(1000.0, "A100", "enterprise") == pytest.approx(1.3875)
    assert stripe.estimate_job_cost(1000.0, "H100", "pro") == pytest.approx(1.389)
    assert stripe.estimate_job_cost(1000.0, "A100", "free") == 0.0
    assert stripe.estimate_job_cost(1000.0, "GTX1080", "pro") == pytest.approx(0.555)
    assert stripe.estimate_job_cost(1000.0, "A100", "gold") == pytest.approx(0.555)
    assert stripe.get_balance("org_unknown") == 0


def test_webhook_simulator_emits_events_and_pays_invoice():
    stripe = StripeIntegration()
    simulator = WebhookSimulator(stripe)
    created = simulator.simulate_subscription_created("org_acme", "pro")

    assert created["type"] == "customer.subscription.created"
    assert created["tier"] == "pro"
    assert stripe.get_subscription("org_acme").id == created["sub_id"]

    invoice = stripe.generate_invoice("org_acme")
    event = simulator.simulate_payment_success("org_acme", invoice.id, 0)

    assert event["type"] == "payment_intent.succeeded"
    assert event["amount_cents"] == 0
    assert invoice.status == "paid"
    assert simulator.get_events("org_acme") == [created, event]
    assert simulator.get_events("org_other") == []
    assert len(simulator.get_events()) == 2
    assert created["org_id"] == "org_acme"
    assert time.time() >= invoice.created_at
