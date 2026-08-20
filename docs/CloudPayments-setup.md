# CloudPayments Billing Setup

> Активация продакшен-биллинга ROMA через CloudPayments.

## Предварительные условия

1. **Получить ключи** от CloudPayments:
   - Зайти на https://my.cloudpayments.ru/ → Integration keys
   - Скопировать: Public ID, API Secret
   - Webhook Secret = API Secret (если не указан отдельно)

2. **Внести ключи** в `.env`:
   ```bash
   CLOUDPAYMENTS_PUBLIC_ID=pk_xxxxxxxxxxxxxxxx
   CLOUDPAYMENTS_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxx
   CLOUDPAYMENTS_WEBHOOK_SECRET=xxxxxxxxxxxxxxxxxxxxxxxx
   CLOUDPAYMENTS_MODE=live
   ```

3. **Настроить вебхук** в CloudPayments Dashboard:
   - URL: `https://roma-execution-bridge-asurdev.zocomputer.io/webhooks/cloudpayments`
   - События: Check, Pay, Fail, Recurrent, Cancel

## Быстрая активация

```bash
cd roma-execution-bridge
./deploy/scripts/activate_cloudpayments.sh
```

Скрипт проверит ключи, перезапустит ROMA и проверит health.

## Тестовый режим

Если ключи не заданы, биллинг работает в тестовом режиме:
- `CLOUDPAYMENTS_MODE=test` (по умолчанию)
- Вебхук возвращает `{"code": 0}` без обработки
- Записи в `processed_invoices` не создаются

## Тестовый вебхук

```bash
# С фейковой подписью (тестовый режим)
curl -X POST http://localhost:8900/webhooks/cloudpayments \
  -H 'Content-Type: application/json' \
  -H 'X-CloudPayments-HMAC-SHA256: test-signature' \
  -d '{
    "InvoiceId": "test-123",
    "Status": "Completed",
    "Amount": 50.00,
    "Currency": "RUB",
    "AccountId": "test@example.com",
    "TestMode": 1
  }'
```

## Идемпотентность

- `processed_invoices` — журнал обработанных invoice_id
- Повторная отправка того же `InvoiceId` игнорируется
- Защита от двойного списания

## Верификация подписи

- Алгоритм: HMAC-SHA256
- Заголовок: `Content-HMAC` or `X-Content-HMAC`
- Проверка через `cloudpayments_client.verify_webhook()`
- При отсутствии ключей — fallback на `api_secret`
