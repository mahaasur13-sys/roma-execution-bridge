# Архитектура ROMA Execution Bridge

## Обзор

ROMA — это FastAPI-сервис на Python, запущенный как постоянный Zo Service. Обрабатывает REST API-запросы для отправки, отслеживания и биллинга GPU/CPU-задач.

## Компоненты

```
                        ┌──────────────────────────┐
                        │   Zo Supervisord         │
                        │   (auto-restart)         │
                        └──────────┬───────────────┘
                                   │
                        ┌──────────▼───────────────┐
                        │   ROMA Service           │
                        │   Port 8900 (internal)   │
                        │   Public HTTPS via       │
                        │   zo.computer.io proxy   │
                        └──────────┬───────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                    │
     ┌────────▼────────┐  ┌────────▼────────┐  ┌────────▼────────┐
     │   Auth Layer    │  │   Middleware     │  │   Endpoints     │
     │   X-API-Key     │  │   Tracking +     │  │   /submit       │
     │   verify_api_key│  │   Metrics +      │  │   /status/:id   │
     │                 │  │   JSON Logging   │  │   /cancel/:id   │
     └─────────────────┘  └─────────────────┘  │   /jobs         │
                                               │   /health       │
                                               │   /metrics      │
                                               └─────────────────┘
```

### 1. FastAPI Application (`main.py`)

Единый файл приложения. Включает:
- **Модели** — Pydantic v2 (`RomaTaskInput`, `RomaTaskResponse`, `RomaStatusResponse`)
- **In-Memory Storage** — `jobs: dict` (временное, будет заменено на SQLite/PostgreSQL)
- **Middleware** — HTTP-трекинг: метрики Prometheus + JSON-логирование каждого запроса

### 2. Аутентификация

Заголовок `X-API-Key` проверяется FastAPI-зависимостью `verify_api_key()`. Ключи хранятся в `config/api_keys.json`.

- `/health` и `/metrics` — публичные (без ключа)
- Все остальные эндпоинты требуют валидный ключ

### 2.1. Мультитенантность

Каждый API-ключ привязан к `tenant_id`. Все данные изолированы по tenant:

- **При создании задачи** — сохраняется `tenant_id` в запись job
- **При чтении статуса** — возвращается 404, если задача принадлежит другому tenant
- **При отмене** — аналогично, 404 для чужих задач
- **GET /jobs** — возвращает только задачи текущего tenant

Попытка доступа к чужой задаче возвращает **404** (не 403) — чтобы не раскрывать существование `job_id` в других tenant'ах.

Файл ключей: `config/api_keys.json` (ключ → {tenant_id, name})

### 3. Очередь задач

- Каждая задача получает `job_id` (UUID v4)
- Статусы: `queued` → выполняется → `completed` / `cancelled`
- Пока хранение в памяти — состояние не переживает перезапуск

### 4. Метрики и логирование

**Prometheus `/metrics`:**
- `roma_jobs_total` — счётчик всех задач
- `roma_jobs_active` — текущие активные
- `roma_queue_depth` — глубина очереди
- `roma_requests_total` — счётчик HTTP-запросов с лейблами
- `roma_request_duration_seconds` — гистограмма длительности

**JSON-логирование:**
- Каждый запрос логируется в JSON: `timestamp`, `level`, `endpoint`, `method`, `status_code`, `duration_ms`, `api_key` (маскированный)
- Уровни: `INFO` (2xx/3xx), `WARNING` (4xx), `ERROR` (5xx)
- Логи доступны в `/dev/shm/roma-execution-bridge.log`

### 5. Развёртывание на Zo

```
Zo Service: svc_r5w3dVvwVPI
Mode:       HTTP (public)
Port:       8900 (внутренний) → zo.computer.io прокси (внешний)
URL:        https://roma-execution-bridge-asurdev.zocomputer.io
Auto-start: ✅ (поднимается после перезапуска Zo)
```

## Стек технологий

| Слой | Технология |
|------|------------|
| Рантайм | Python 3.12 |
| Веб-фреймворк | FastAPI + Pydantic v2 |
| ASGI-сервер | Uvicorn |
| Метрики | prometheus_client |
| Логирование | Python logging (JSON formatter) |
| Аутентификация | X-API-Key (FastAPI Header dependency) |
| Хранение | In-memory dict → SQLite → PostgreSQL |
| Платформа | Zo Computer (Debian 12, supervisord) |

## План развития

- **Фаза 0 (текущая):** Pre-Launch — аутентификация, метрики, документация
- **Фаза 1:** Multi-tenancy, Stripe-биллинг, дашборд
- **Фаза 2:** GPU-воркеры, K8s-оператор, production-кластер

---

## Биллинг (новое)

```
┌─────────────────────────────────────────────┐
│              BILLING LAYER                   │
│                                              │
│  POST /submit ──► check limits ──► 402?     │
│       │                    │                 │
│       ▼                    ▼                 │
│  usage.json         plans.json               │
│  (tenant counters)  (Free/Pro/Enterprise)     │
│                                              │
│  Stripe (stub)                               │
│  POST /billing/create-checkout-session       │
│       └──► Returns URL or setup instructions │
└─────────────────────────────────────────────┘
```

- **Usage Tracking:** `config/usage.json` — счётчики `total_jobs`, `total_gpu_seconds` на tenant
- **Plans:** `config/plans.json` — Free (50 jobs) / Pro (1000) / Enterprise (unlimited)
- **Лимиты:** превышение → 402 Payment Required
- **Stripe:** заглушка при отсутствии ключей, реальный Checkout при `STRIPE_SECRET_KEY`

## Наблюдаемость

- **Jaeger** — distributed tracing (OpenTelemetry + Jaeger exporter), auto-instrumentation FastAPI + user spans
- **Prometheus** — 3 alert rules: high request rate, high active jobs, high error rate (>5%)
- **Alertmanager** — Telegram webhook integration
- **Logs** — JSON structured logging (все логи через Loki + Grafana)
