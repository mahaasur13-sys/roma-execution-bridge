# CloudPayments Live Test — Чек-лист

**Дата проверки:** ________  
**Тестировщик:** ________  
**Ожидаемое время:** 1 час

## 0. Предварительные условия

- [ ] Аккаунт CloudPayments (https://my.cloudpayments.ru)
- [ ] `CLOUDPAYMENTS_PUBLIC_ID` (начинается с `pk_`)
- [ ] `CLOUDPAYMENTS_API_SECRET` (64 hex символа)
- [ ] Тестовая карта: `5555 5555 5555 4444` (MasterCard, всегда успех)
- [ ] Или `4242 4242 4242 4242` (Visa)

## 1. Установка ключей

```bash
# В Zo: Settings → Advanced → Secrets → добавить:
#   CLOUDPAYMENTS_PUBLIC_ID=pk_xxxxxxxxxxxxxxxx
#   CLOUDPAYMENTS_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Проверить
cd /home/workspace/roma-execution-bridge
source .env 2>/dev/null
echo "PUBLIC_ID: ${CLOUDPAYMENTS_PUBLIC_ID:0:8}***"
echo "API_SECRET: ${CLOUDPAYMENTS_API_SECRET:0:8}***"
```

- [ ] Ключи установлены
- [ ] Сервис ROMA перезапущен

## 2. Health Check после перезапуска

```bash
curl -s https://roma-execution-bridge-asurdev.zocomputer.io/health | python3 -m json.tool
```

- [ ] `cloudpayments_enabled: true`

## 3. Создание заказа (Checkout)

```bash
# Pro план (4900 RUB)
curl -s -X POST https://roma-execution-bridge-asurdev.zocomputer.io/billing/create-checkout-session \
  -H "X-API-Key: roma-demo-key-2026" \
  -H "Content-Type: application/json" \
  -d '{"plan":"pro","email":"test@example.com"}' | python3 -m json.tool
```

**Ожидаемый ответ:**
```json
{
  "url": "https://widget.cloudpayments.ru/...",
  "session_id": "12345",
  "plan": "pro",
  "tenant_id": "tenant-demo",
  "dry_run": false
}
```

- [ ] Возвращает `url` (не example.com)
- [ ] `dry_run: false`
- [ ] Открыть URL в браузере → форма оплаты отображается

## 4. Тестовая оплата

- [ ] Ввести тестовую карту: `5555 5555 5555 4444`
- [ ] CVC: любой (123)
- [ ] Срок: любой будущий (12/28)
- [ ] Нажать «Оплатить»
- [ ] Видим страницу успешной оплаты

## 5. Проверка Webhook

```bash
# Проверить логи сервиса
tail -20 /dev/shm/roma-execution-bridge.log | grep -i "cloudpayments\|webhook\|Pay\|Completed"
```

**Ожидаемый лог:**
```
CloudPayments webhook: Pay tenant=tenant-demo plan=pro
CloudPayments: subscription activated — tenant-demo → pro
```

- [ ] Webhook получил событие `Pay` / `Completed`
- [ ] Подписка активирована в логах

## 6. Проверка статуса подписки в БД

```bash
cd /home/workspace/roma-execution-bridge
python3 -c "
import db; db.init_db()
t = db.get_tenant('tenant-demo')
print(f\"Plan: {t.get('plan')}\")
print(f\"Status: {t.get('subscription_status')}\")
print(f\"End date: {t.get('subscription_end_date')}\")
"
```

- [ ] `plan: pro`
- [ ] `subscription_status: active`

## 7. Проверка лимитов

```bash
# До активации — задания блокировались?
# После активации Pro — должны приниматься
curl -s -X POST https://roma-execution-bridge-asurdev.zocomputer.io/submit \
  -H "X-API-Key: roma-demo-key-2026" \
  -H "Content-Type: application/json" \
  -d '{"task":"test live billing","gpu_required":false}' | python3 -m json.tool
```

- [ ] Задание принято (статус 202)
- [ ] Нет ошибки «no active subscription»

## 8. Проверка метрик

```bash
curl -s https://roma-execution-bridge-asurdev.zocomputer.io/metrics | grep cloudpayments
```

- [ ] `roma_cloudpayments_checkouts_total{plan="pro"} 1`
- [ ] `roma_cloudpayments_success_total 1`

## 9. Откат (если нужно)

```bash
# Сбросить подписку tenant-demo на free
cd /home/workspace/roma-execution-bridge
python3 -c "
import db; db.init_db()
db.update_tenant_subscription('tenant-demo', '', '', 'active', 'free', None)
print('Reset to free')
"
```

- [ ] Подписка сброшена

## Результат

| Шаг | Статус | Примечание |
|-----|:------:|-----------|
| 0. Ключи установлены | ⬜ | |
| 1. Health `cloudpayments_enabled` | ⬜ | |
| 2. Checkout возвращает URL | ⬜ | |
| 3. Тестовая оплата успешна | ⬜ | |
| 4. Webhook обработан | ⬜ | |
| 5. БД: plan=pro, status=active | ⬜ | |
| 6. Задания принимаются | ⬜ | |
| 7. Метрики обновлены | ⬜ | |

**Подпись:** ________  
**Дата:** ________
