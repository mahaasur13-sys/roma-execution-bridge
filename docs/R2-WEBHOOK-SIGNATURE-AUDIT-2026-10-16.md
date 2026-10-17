# R2 Webhook Signature — Read-only Audit

Дата: 2026-10-16. Статус: **read-only разбор** — R2 остаётся заблокированным.
Единственное изменение кода в рамках этого разбора: fail-closed в Stripe
(`return True` → `return False`). Остальное — анализ без правок.

## 1. Резюме

R2 (billing) заблокирован, потому что формат подписи webhook-провайдеров в коде
не сверен с реальностью провайдера:

- **CloudPayments** (основной): `verify_webhook` использует **hex**-дайджест и отдельный
  `CLOUDPAYMENTS_WEBHOOK_SECRET`, тогда как спецификация — **Base64** + **API Secret**.
  Докстринг самого метода прямо помечает формат как `UNVERIFIED`.
- **Stripe** (legacy): верификатор был fail-open (исправлено на fail-closed), но формат
  (`Stripe-Signature: t=,v1=`) по-прежнему не реализован, и метод не вызывается в живом пути.

## 2. CloudPayments (основной)

Файлы: `routers/webhooks.py` (обработчик), `billing/cloudpayments_client.py::verify_webhook`.

Код делает:

- `hmac.new(secret.encode(), raw_body, sha256).hexdigest()` — **hex**, не Base64.
- ключ — `CLOUDPAYMENTS_WEBHOOK_SECRET` (не API Secret; API Secret явно не fallback).
- обработчик читает `Content-HMAC` / `Content-Hmac` / `X-Content-HMAC`, проверяет оба
  против одного и того же `raw_body`, затем `json.loads(raw_body)`.
- идемпотентность по `InvoiceId` (не `TransactionId`).
- ✅ `compare_digest`; ✅ fail-closed на missing sig/secret; ✅ секрет не логируется.

Сверка:

| Пункт | Ожидание | Код | Статус |
|-------|----------|-----|--------|
| Кодировка дайджеста | Base64 | hex (`hexdigest`) | ❌ |
| Ключ | API Secret | `CLOUDPAYMENTS_WEBHOOK_SECRET` | ❌ |
| Имя заголовка | офиц. не зафиксировано; `Content-HMAC-SHA256` — гипотеза из неофиц. гайда | читает `Content-HMAC`/`X-Content-HMAC` | ⚠️ уточнить |
| Content-HMAC vs X-Content-HMAC (raw vs URL-decoded) | разные тела | оба против одного `raw_body` | ⚠️ |
| Replay-защита | по `TransactionId` | по `InvoiceId` | ⚠️ |

## 3. Stripe (legacy)

Файл: `billing/stripe_client.py::verify_webhook_signature`.

Состояние:

- ✅ **fail-open исправлен**: без `webhook_secret` → `return False`.
- ❌ **Формат всё ещё неверный**: не парсит `t=`/`v1=`, подписывает `payload` вместо
  `"{t}.{payload}"`, сравнивает hex-дайджест с полным заголовком `t=...,v1=...`, нет
  окна по `t` (300 с), не перебирает несколько `v1`.
- ⚠️ **Нигде не вызывается в живом пути**: `verify_webhook_signature` зовёт только
  `StripeWebhookHandler` (line 113), а Stripe-маршрута в `routers/webhooks.py` нет
  (есть только `/webhooks/cloudpayments` и `/webhooks/email`).

**Решение (отдельное, вне этого разбора):** чинить формат или удалять
верификатор/клиент. Чинить имеет смысл только если Stripe реально вернётся в
использование.

## 4. Что нужно для разблокировки CloudPayments

Захватить **один реальный нотификат** (тестовый режим, `CLOUDPAYMENTS_MODE=test`) и
зафиксировать:

1. **Имя заголовка** с подписью (какой именно: `Content-HMAC`, `Content-HMAC-SHA256`,
   `X-Content-HMAC` — и есть ли оба).
2. **Кодировку дайджеста** — Base64 или hex.
3. **Каким ключом подписано** — API Secret или отдельный webhook secret.
4. **Сырое тело** (raw bytes) — до любого парсинга.
5. **URL-декодированное тело** — чтобы проверить различие `Content-HMAC` (raw) vs
   `X-Content-HMAC` (decoded).
6. (для idempotency) полный набор полей события, включая `TransactionId`.

По этим пунктам правка `verify_webhook` уложится в небольшой дифф + тест на
захваченном примере.

## 5. Официальная документация

- **CloudPayments** — официальный портал: https://developers.cloudpayments.ru/
  (раздел «Уведомления о платеже»). **НЕОФИЦИАЛЬНЫЙ** community-гайд (из него взята
  только гипотеза «Base64 + API Secret + заголовок `Content-HMAC-SHA256`»):
  https://tunnelhunt.ru/blog/cloudpayments-webhooks-local-testing/
- **Stripe** — официально: https://docs.stripe.com/webhooks/signature
