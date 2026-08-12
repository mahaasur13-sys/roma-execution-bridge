# ROMA Billing — Stripe Integration

## Overview

ROMA uses **Stripe** for subscription billing. Each tenant has a plan (Free / Pro / Enterprise) that determines:
- Maximum jobs per month
- GPU priority
- Support tier

## Plans

| Plan | Price | Max Jobs/Month | GPU Priority | Stripe Price ID |
|------|-------|----------------|-------------|-----------------|
| **Free** | $0 | 50 | Low | — (no Stripe) |
| **Pro** | $49/mo | 1 000 | Normal | `price_pro` (env) |
| **Enterprise** | $299/mo | Unlimited | High | `price_enterprise` (env) |

## Architecture

```
Tenant signs up
    │
    ▼
/api_key_manager → assigns tenant_id + plan=free
    │
    ▼
GET /billing/create-checkout-session?plan=pro
    │
    ▼
Stripe Checkout → tenant pays → webhook fires
    │
    ▼
POST /webhooks/stripe → checkout.session.completed
    │
    ▼
tenant.subscription_status = "active"
tenant.plan = "pro"
```

## Data Storage

All subscription data is in SQLite (`data/roma.db`):

```sql
CREATE TABLE tenants (
    tenant_id TEXT PRIMARY KEY,
    api_key TEXT,
    name TEXT,
    plan TEXT DEFAULT 'free',
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    subscription_status TEXT DEFAULT 'inactive',
    subscription_end_date TEXT,
    max_jobs_per_month INTEGER DEFAULT 50,
    created_at TEXT,
    updated_at TEXT
);
```

## Checkout Flow

### Create Session

```
POST /billing/create-checkout-session
X-API-Key: roma-demo-key-2026

{
  "plan": "pro",
  "success_url": "https://example.com/success",
  "cancel_url": "https://example.com/cancel"
}
```

Returns:
```json
{
  "session_id": "cs_test_a1b2c3...",
  "url": "https://checkout.stripe.com/c/pay/cs_test_a1b2c3..."
}
```

### Checkout Flow

1. **User visits** `session.url` → redirected to Stripe-hosted checkout page
2. **User enters** card details (Stripe handles PCI compliance)
3. **On success** → Stripe redirects to `success_url?session_id=cs_test_...`
4. **Webhook fires** → ROMA receives `checkout.session.completed`
5. **Tenant updated** → `subscription_status = "active"`, plan upgraded

## Webhook Events

See [docs/webhooks.md](webhooks.md) for full event reference.

| Event | Action |
|-------|--------|
| `checkout.session.completed` | Activate subscription, set `stripe_customer_id` + `stripe_subscription_id` |
| `invoice.payment_succeeded` | Update `subscription_end_date`, reset usage counter |
| `invoice.payment_failed` | Mark `subscription_status = "past_due"`, block job creation |
| `customer.subscription.deleted` | Mark `subscription_status = "canceled"`, block access |

## Subscription Statuses

| Status | Can Submit Jobs? | Description |
|--------|-----------------|-------------|
| `active` | ✅ Yes | Active subscription, limits apply |
| `trialing` | ✅ Yes | Trial period, limits apply |
| `inactive` | ⚠️ Free tier only | No paid subscription |
| `past_due` | ❌ No (402) | Payment failed, access blocked |
| `canceled` | ❌ No (402) | Subscription ended, access blocked |

## Environment Variables

```bash
# Stripe API Keys
STRIPE_SECRET_KEY=sk_test_...
STRIPE_PUBLISHABLE_KEY=pk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...

# Stripe Price IDs
STRIPE_PRICE_PRO=price_...
STRIPE_PRICE_ENTERPRISE=price_...
```

## Billing-Disabled Mode

If `STRIPE_SECRET_KEY` is not set, the service runs in **billing-disabled mode**:
- `/billing/create-checkout-session` returns a stub message
- `/webhooks/stripe` returns 200 (no-op)
- All tenants default to plan=free, subscription_status=active
- No Stripe calls are made

## Stripe Setup

1. **Create products** in [Stripe Dashboard → Products](https://dashboard.stripe.com/products):
   - Pro: $49/month (recurring)
   - Enterprise: $299/month (recurring)

2. **Copy Price IDs** to environment variables:
   ```bash
   STRIPE_PRICE_PRO=price_1ABC...
   STRIPE_PRICE_ENTERPRISE=price_2DEF...
   ```

3. **Create webhook endpoint** in [Stripe Dashboard → Webhooks](https://dashboard.stripe.com/webhooks):
   - URL: `https://roma-execution-bridge-asurdev.zocomputer.io/webhooks/stripe`
   - Events: `checkout.session.completed`, `invoice.payment_succeeded`, `invoice.payment_failed`, `customer.subscription.deleted`
   - Copy signing secret to `STRIPE_WEBHOOK_SECRET`

4. **Test locally**:
   ```bash
   stripe listen --forward-to localhost:8900/webhooks/stripe
   stripe trigger checkout.session.completed
   ```
