# DecisionOS v1.0.0 — Transfer Context for New Chat

> **Скопируй этот файл целиком в тело первого сообщения нового чата.**
> Он содержит всё, что нужно знать новому ассистенту для продолжения работы.

## 🎯 Что такое DecisionOS

**Enterprise multi-tenant SaaS decision infrastructure**, выросшая из ROMA Execution Bridge.
Правило гейтирования: `ExecutionAllowed = Quota OK ∧ Cost OK ∧ Policy OK`.

Тиры: **Start / Pro / Enterprise** с лимитами по jobs, GPU, concurrency, retention, white-label, SSO.

---

## 📦 Созданные модули (32/32 теста зелёные)

```
roma-execution-bridge/
├── crypto_payments/           # ✅ 10/10 тестов
│   ├── models.py              # Pydantic v2: CryptoInvoice, CryptoPayment, CryptoWebhookEvent
│   ├── db_models.py           # SQLAlchemy 2.0 async ORM
│   ├── settings.py            # CryptoSettings (env: CRYPTO_)
│   ├── provider.py            # CryptoPaymentProvider (ABC) + NOWPaymentsProvider
│   ├── service.py             # CryptoInvoiceService
│   ├── webhooks.py            # CryptoWebhookHandler
│   ├── router.py              # POST /v1/crypto/invoices, GET .../{id}, POST .../webhooks/{provider}
│   ├── migrations/002_crypto_payments.sql  # 3 таблицы
│   └── wallets/               # ✅ 10/10 тестов
│       ├── models.py          # CryptoWallet, DepositAddress, MoneroViewOnlyConfig, WalletRotationEvent
│       ├── db_models.py       # 4 SQLAlchemy ORM-модели
│       ├── service.py         # CryptoWalletService
│       ├── monero_adapter.py  # MoneroWalletAdapter (monero-wallet-rpc, view-only)
│       ├── provider_wallet_adapter.py  # NOWPayments/Heleket/CryptoCloud/BTCPay
│       ├── router.py          # POST /v1/crypto/wallets + addresses + monero/subaddress + rotate
│       └── migrations/003_crypto_wallets.sql  # 4 таблицы
│
├── support_chat/              # ✅ 12/12 тестов
│   ├── models.py              # SupportTicket, ChatMessage, ChatParticipant, TicketAttachment, CsatRating
│   ├── db_models.py           # 5 SQLAlchemy ORM-моделей
│   ├── settings.py            # SupportSettings (env: SUPPORT_)
│   ├── service.py             # SupportTicketService (CRUD, transitions, CSAT, agent assignment)
│   ├── chat_service.py        # ChatService + WebSocket ConnectionManager
│   ├── router.py              # REST CRUD + WebSocket /ws/support/{ticket_id}
│   └── migrations/004_support_chat.sql  # 5 таблиц
│
├── landing/                   # 🚧 В процессе
│   └── logos/                 # ✅ 5 SVG-вариантов логотипа (blue, purple, green, mono, icon)
│
├── policy_engine.py           # 15+ actions (crypto, wallet, support)
├── audit_events.py            # 12+ типов событий
├── error_model.py             # 20+ machine-readable кодов ошибок
├── main.py                    # Все роутеры зарегистрированы
├── .env.crypto.example        # Конфигурация crypto + wallets + support
│
├── tests/
│   ├── test_crypto_payments.py    # 10/10 ✅
│   ├── test_crypto_wallets.py     # 10/10 ✅ (3 Monero-specific)
│   └── test_support_chat.py       # 12/12 ✅ (WebSocket + Monero-контекст)
│
└── docs/
    ├── STRATEGIC-REPORT-DECISIONOS-v1.0.0.md   # Полный стратегический отчёт
    ├── STRATEGIC-REPORT-DECISIONOS-v1.0.0.pdf  # PDF-версия (44 КБ)
    ├── RELEASE-NOTES.md
    ├── WEEK4-RUNBOOK.md
    └── WEEK4-SMOKE.md
```

---

## 🔗 Ключевые архитектурные решения

1. **Policy Engine** — `evaluate_policies(tenant_id, action, context) → {result, reason, policy_name}`
2. **Decision Gate** — тройной guard: Quota ∧ Cost ∧ Policy
3. **Tier Profiles** — Start (5 crypto-инвойсов/мес) / Pro (20) / Enterprise (100)
4. **Audit Trail** — все решения + crypto-платежи + wallet-события + support-тикеты → PostgreSQL
5. **Multi-tenant isolation** — tenant-level через заголовки `x-tenant-id` / `x-api-key`
6. **Monero Privacy** — view-only wallets, никогда не храним spend keys, subaddresses per invoice
7. **WebSocket** — `/ws/support/{ticket_id}` с real-time broadcast через ConnectionManager

---

## 🧪 Запуск тестов

```bash
cd /home/workspace/roma-execution-bridge

# Все 32 теста
python -m pytest tests/test_crypto_payments.py tests/test_crypto_wallets.py tests/test_support_chat.py -v

# Только crypto payments
python -m pytest tests/test_crypto_payments.py -v

# Только wallets
python -m pytest tests/test_crypto_wallets.py -v

# Только support chat
python -m pytest tests/test_support_chat.py -v
```

**Текущий статус: 32/32 passed** ✅

---

## 📋 API-эндпоинты

### Crypto Payments
```
POST /v1/crypto/invoices
GET  /v1/crypto/invoices/{invoice_id}
POST /v1/crypto/webhooks/{provider}
```

### Crypto Wallets
```
POST /v1/crypto/wallets
POST /v1/crypto/wallets/{id}/addresses
POST /v1/crypto/wallets/{id}/monero/subaddress
POST /v1/crypto/wallets/{id}/rotate
```

### Support Chat
```
POST   /v1/support/tickets
GET    /v1/support/tickets
GET    /v1/support/tickets/{ticket_id}
POST   /v1/support/tickets/{ticket_id}/messages
POST   /v1/support/tickets/{ticket_id}/assign
POST   /v1/support/tickets/{ticket_id}/transition
POST   /v1/support/tickets/{ticket_id}/csat
WS     /v1/support/ws/{ticket_id}
```

---

## 🚧 Что осталось доделать

### P0 — Production Hardening
- [ ] Stripe Checkout / CloudPayments live payment flow
- [ ] Enterprise SSO (OIDC/SAML)
- [ ] PostgreSQL → TimescaleDB hypertables
- [ ] SLO/SLI дашборд Grafana
- [ ] Rate limiting per-tenant

### P1 — GTM
- [ ] Plugin marketplace MVP
- [ ] White-label onboarding flow (Enterprise)
- [ ] AI chat assistant для policy authoring
- [ ] Multi-region deployment (EU, US)

### P2 — Landing Page
- [ ] `index.html` — главная страница (hero, features, AI chat, code snippets)
- [ ] `pricing.html` — таблица тарифов
- [ ] `docs.html` — документация API
- [ ] `dashboard.html` — макет админ-панели
- [ ] `css/style.css` — общие стили (тёмная тема, Tailwind)
- [ ] `README.md` — инструкция по деплою (Vercel/Netlify)

---

## 🔑 Ключевые файлы для быстрого старта

| Файл | Зачем |
|------|-------|
| `file docs/STRATEGIC-REPORT-DECISIONOS-v1.0.0.md` | Полная картина проекта |
| `file policy_engine.py` | Все policy-правила (15+ actions) |
| `file audit_events.py` | Все audit-события (12+ типов) |
| `file error_model.py` | Все коды ошибок (20+) |
| `file main.py` | Регистрация всех роутеров |
| `file .env.crypto.example` | Полная конфигурация |

---

## 📊 Метрики проекта

| Метрика | Значение |
|---------|----------|
| Модулей | 3 (crypto_payments, wallets, support_chat) |
| Python-файлов | 20+ |
| SQL-миграций | 3 (002, 003, 004) |
| Pydantic-моделей | 30+ |
| API-эндпоинтов | 10 REST + 1 WebSocket |
| Policy-actions | 15+ |
| Audit-событий | 12+ |
| Error-кодов | 20+ |
| Тестов | 32/32 ✅ |
| Строк кода | ~8 000+ |

---

**Файл создан:** 2026-08-19  
**Для использования:** скопируй весь файл в первое сообщение нового чата
