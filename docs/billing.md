# ROMA Billing — Stripe Live Setup

## Quick Start

ROMA uses Stripe for subscription billing. This guide covers moving from test mode to live payments.

## 1. Stripe Dashboard Setup

### Create Live Products & Prices

1. Go to [Stripe Dashboard → Products](https://dashboard.stripe.com/products)
2. Toggle **"Test mode" to OFF** (top-right corner)
3. Create two products:

| Product       | Price / Month | Price ID env var          |
|---------------|---------------|---------------------------|
| ROMA Pro      | $49           | `STRIPE_PRICE_PRO`        |
| ROMA Enterprise | $299        | `STRIPE_PRICE_ENTERPRISE` |

4. For each product, create a **Recurring** price (monthly, USD)
5. Copy the Price IDs (e.g., `price_1ABC123xyz`)
6. Save them in `.env`

### Get Live API Keys

1. Go to [Stripe Dashboard → API Keys](https://dashboard.stripe.com/apikeys)
2. Click **"Reveal live key"**
3. Copy:
   - **Secret key** → `STRIPE_SECRET_KEY=sk_live_...`
   - **Publishable key** → `STRIPE_PUBLISHABLE_KEY=pk_live_...`

### Create Webhook Endpoint

1. Go to [Stripe Dashboard → Webhooks](https://dashboard.stripe.com/webhooks)
2. Click **"Add endpoint"**
3. Endpoint URL: `https://roma-execution-bridge-asurdev.zocomputer.io/webhooks/stripe`
4. Events to listen for:
   - `checkout.session.completed`
   - `customer.subscription.deleted`
   - `invoice.payment_succeeded`
   - `invoice.payment_failed`
5. After creation, click **"Reveal"** to copy Signing Secret → `STRIPE_WEBHOOK_SECRET=whsec_...`

## 2. Environment Variables

Add to `.env` (or Zo Secrets in Settings → Advanced):

```bash
STRIPE_SECRET_KEY=sk_live_your_secret_key
STRIPE_PUBLISHABLE_KEY=pk_live_your_publishable_key
STRIPE_WEBHOOK_SECRET=whsec_your_webhook_secret
STRIPE_PRICE_PRO=price_your_pro_price_id
STRIPE_PRICE_ENTERPRISE=price_your_enterprise_price_id
```

## 3. Verify

```bash
# Service should report billing enabled
curl https://roma-execution-bridge-asurdev.zocomputer.io/health
# → {"billing": {"stripe_enabled": true}}

# Create a checkout session
curl -X POST https://roma-execution-bridge-asurdev.zocomputer.io/billing/create-checkout-session \
  -H "X-API-Key: roma-demo-key-2026" \
  -H "Content-Type: application/json" \
  -d '{"plan": "pro"}'
# → {"status": "checkout_created", "url": "https://checkout.stripe.com/..."}
```

## 4. Testing with Live Keys

Stripe allows test card `4242424242424242` even in **live mode** — no real charges are processed until you use a real card.

## Graceful Degradation

If `STRIPE_SECRET_KEY` is not set:
- `/billing/create-checkout-session` returns `"status": "billing_disabled"` with clear instructions
- Stripe pricing API is not imported
- No Stripe SDK errors occur

## Stripe Webhook Events Handled

| Event                         | Action in ROMA                                                     |
|-------------------------------|--------------------------------------------------------------------|
| `checkout.session.completed`  | Set `tenants.subscription_status = "active"`, save subscription ID |
| `customer.subscription.deleted` | Set `tenants.subscription_status = "inactive"`, clear dates      |
| `invoice.payment_succeeded`   | Log success, update subscription end date                         |
| `invoice.payment_failed`      | Set `tenants.subscription_status = "past_due"`                    |

## Troubleshooting

**Webhook returns 400:** Check `STRIPE_WEBHOOK_SECRET` matches the dashboard signing secret.

**Checkout returns 500:** Verify `STRIPE_PRICE_PRO` / `STRIPE_PRICE_ENTERPRISE` contain valid Price IDs in the correct Stripe mode (test IDs won't work with live keys).

**Tenant stays inactive after payment:** Check webhook logs — Stripe may not have reached the endpoint. Use `stripe listen --forward-to http://localhost:8900/webhooks/stripe` for local debugging.
