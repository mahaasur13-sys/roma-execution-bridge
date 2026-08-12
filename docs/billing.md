# Биллинг и тарифные планы

## Обзор

ROMA использует простую модель тарификации: **количество задач в месяц** + **GPU-часы** (эстимированные). Каждый tenant имеет тарифный план с лимитами.

## Тарифные планы

| План | Задач/мес | Цена | Особенности |
|------|:---------:|------|-------------|
| **Free** | 50 | $0 | Community support |
| **Pro** | 1 000 | $49/мес | Priority queue, Email support |
| **Enterprise** | Unlimited | $299/мес | Dedicated GPU, SSO, SLA 99.9% |

Конфигурация: `config/plans.json`

## Как это работает

### 1. Учёт использования

При каждой успешной задаче (`POST /submit`) счётчик `total_jobs` увеличивается для текущего tenant. GPU-задачи также считают `total_gpu_seconds` (пока 300 сек на задачу — эстимейт).

Данные хранятся в `config/usage.json`:

```json
{
  "tenant-demo": {
    "total_jobs": 50,
    "total_gpu_seconds": 0,
    "last_updated": "2026-08-12T08:01:38.716086"
  }
}
```

### 2. Проверка лимитов

Перед созданием задачи проверяется:
- Текущий план tenant'а
- Текущее использование
- Если лимит превышен → **402 Payment Required**

```
POST /submit → 402
{
  "detail": "Plan 'free' limit reached: 50/50 jobs. Upgrade at ..."
}
```

### 3. Stripe-платежи

Stripe находится в режиме **заглушки** (ключи не настроены). При попытке создать checkout-сессию возвращается инструкция по настройке:

```
POST /billing/create-checkout-session → 200
{
  "status": "billing_disabled",
  "message": "Stripe is not configured. To enable billing:\n...",
  "plan": "pro"
}
```

**Чтобы включить Stripe:**

1. Добавить `STRIPE_SECRET_KEY` в [Zo Secrets](/?t=settings&s=advanced)
2. Добавить `STRIPE_PUBLISHABLE_KEY` для фронтенда
3. Перезапустить сервис ROMA

## API

### GET /usage

Возвращает использование для текущего tenant.

```bash
curl -H "X-API-Key: roma-demo-key-2026" \
  https://roma-execution-bridge-asurdev.zocomputer.io/usage
```

**Ответ:**
```json
{
  "tenant_id": "tenant-demo",
  "plan": "free",
  "usage": {
    "total_jobs": 50,
    "total_gpu_seconds": 0,
    "last_updated": "2026-08-12T08:01:38"
  },
  "limits": {
    "max_jobs_per_month": 50,
    "max_jobs_per_month_display": "50"
  }
}
```

### POST /billing/create-checkout-session

Создаёт Stripe Checkout Session (или заглушку, если Stripe не настроен).

```bash
curl -X POST https://roma-execution-bridge-asurdev.zocomputer.io/billing/create-checkout-session \
  -H "X-API-Key: roma-demo-key-2026" \
  -H "Content-Type: application/json" \
  -d '{"plan": "pro"}'
```

## Дорожная карта биллинга

- [x] Учёт использования (usage.json)
- [x] Тарифные планы (plans.json)
- [x] Проверка лимитов (402)
- [x] Stripe-заглушка с инструкцией
- [ ] Реальный Stripe Checkout (нужны ключи)
- [ ] Stripe Webhook для обработки платежей
- [ ] Invoice generation
- [ ] Usage-based pricing (GPU-часы)
- [ ] Dashboard с графиком использования
