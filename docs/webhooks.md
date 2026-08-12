# ROMA Webhooks

Stripe webhook endpoint — `POST /webhooks/stripe`. Публичный (без API-ключа), подпись верифицируется через `STRIPE_WEBHOOK_SECRET`.

## Настройка в Stripe Dashboard

1. Перейдите в [Stripe Dashboard → Webhooks](https://dashboard.stripe.com/webhooks)
2. Добавьте endpoint: `https://roma-execution-bridge-asurdev.zocomputer.io/webhooks/stripe`
3. Events to listen for:
   - `checkout.session.completed`
   - `invoice.payment_succeeded`
   - `invoice.payment_failed`
   - `customer.subscription.deleted`
4. Copy the signing secret (`whsec_...`) → `STRIPE_WEBHOOK_SECRET` в `.env`

## Обрабатываемые события

### `checkout.session.completed`

Триггерится после успешной оплаты. Обновляет статус tenant на `active`, сохраняет `stripe_customer_id` и `stripe_subscription_id`.

**Действия:**
- `subscription_status` → `"active"`
- `stripe_customer_id`, `stripe_subscription_id` записываются в БД
- План обновляется из метаданных сессии

### `invoice.payment_succeeded`

Регулярная успешная оплата (продление). Сбрасывает usage и обновляет даты.

**Действия:**
- `subscription_end_date` обновляется
- Usage сбрасывается для нового цикла

### `invoice.payment_failed`

Платёж не прошёл. Доступ ограничивается.

**Действия:**
- `subscription_status` → `"past_due"`
- При следующем POST `/submit` tenant получит 402

### `customer.subscription.deleted`

Подписка отменена или истекла.

**Действия:**
- `subscription_status` → `"canceled"`
- `stripe_subscription_id` обнуляется
- При POST `/submit` tenant получит 402

## Тестирование вебхуков

```bash
# Setup
stripe login
stripe listen --forward-to localhost:8900/webhooks/stripe

# Trigger test event
stripe trigger checkout.session.completed

# Send test event via curl
curl -X POST http://localhost:8900/webhooks/stripe \
  -H "Content-Type: application/json" \
  -H "Stripe-Signature: test" \
  -d '{"type":"checkout.session.completed","data":{"object":{"metadata":{"tenant_id":"tenant-demo","plan":"pro"},"customer":"cus_test","subscription":"sub_test"}}}'
```

## Безопасность

- Webhook **публичный** (без X-API-Key)
- Подпись верифицируется через `stripe.Webhook.construct_event()`
- Секрет `STRIPE_WEBHOOK_SECRET` никогда не логируется
