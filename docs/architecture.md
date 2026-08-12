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
