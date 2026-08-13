# ROMA Execution Bridge — Production Readiness Checklist

**Версия:** v1.2.0  
**Дата:** 2026-08-13  
**Статус:** ✅ P0+P1+P2 закрыты, готов к деплою

---

## 🔑 Секреты и переменные окружения

- [ ] `CLOUDPAYMENTS_PUBLIC_ID` — публичный ключ CloudPayments
- [ ] `CLOUDPAYMENTS_API_SECRET` — секретный ключ (он же webhook secret)
- [ ] `SENDGRID_API_KEY` — ключ для email-рассылки
- [ ] `FROM_EMAIL` — подтверждённый отправитель в SendGrid
- [ ] `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` — OAuth Google
- [ ] `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` — OAuth GitHub
- [ ] `PYTHONPATH=/home/workspace/roma-execution-bridge`
- [ ] `PYTHONUNBUFFERED=1`
- [ ] Все секреты в `.env` (файл в `.gitignore`, не коммитится)

## 🏥 Health & Observability

- [ ] `GET /health` → 200, `cloudpayments_enabled: true` (если ключи заданы)
- [ ] `GET /metrics` → 200, содержит roma_*, prometheus_metrics
- [ ] Grafana dashboard `ROMA v1.2` импортирован (`deploy/monitoring/grafana/roma-dashboard.json`)
- [ ] Alert rules загружены (`deploy/monitoring/alert-rules-roma.yml`)
- [ ] AlertManager/TG контакт-поинты настроены
- [ ] Jaeger трейсинг (опционально, `JAEGER_ENABLED=true`)

## 💳 Биллинг (CloudPayments)

- [ ] Dry-run: `POST /billing/create-checkout-session` → `dry_run: true` (без ключей)
- [ ] Live: с реальными ключами — возвращает `url` платёжной страницы
- [ ] Webhook URL зарегистрирован в CloudPayments Dashboard:
  `https://roma-execution-bridge-asurdev.zocomputer.io/webhooks/cloudpayments`
- [ ] События: Payment, Recurrent, Fail, Cancel
- [ ] `POST /webhooks/cloudpayments` проверяет HMAC-подпись
- [ ] После успешной оплаты tenant.subscription_status → `active`
- [ ] После отказа → `past_due`
- [ ] После отмены → `canceled`

## 🔐 Безопасность

- [ ] API-ключи не логируются (маскируются: `key[:8]***`)
- [ ] `_admin_only()` проверяет `tenant-demo` — не любой ключ
- [ ] Аналитика фильтруется по tenant_id (нет утечек между tenant'ами)
- [ ] `EmailStr` валидация на `/beta/apply`
- [ ] Parameterized SQL-запросы (нет f-string SQL injection)
- [ ] Соединения SQLite всегда закрываются (try/finally)
- [ ] Rate limiting включён: `/submit`(30/min), `/billing`(10/min), `/admin`(20/min), `/auth`(15/min), `/beta`(5/min)

## 🚦 Эндпоинты (smoke test)

- [ ] `GET /health` → `{"status":"ok"}`
- [ ] `GET /metrics` → 20+ roma_* метрик
- [ ] `POST /submit` → принимает задачу (X-API-Key)
- [ ] `GET /status/{job_id}` → статус задачи
- [ ] `GET /dashboard` → HTML дашборд
- [ ] `GET /workers` → список worker'ов (без 500)
- [ ] `POST /feedback` → сохраняет фидбек
- [ ] `POST /beta/apply` → email-валидация
- [ ] `POST /webhooks/cloudpayments` → 200 без подписи (code:0)
- [ ] `POST /webhooks/email` → 200
- [ ] OAuth: `/auth/login` → кнопки Google/GitHub
- [ ] Admin: `/admin` → 403 для не-admin, 200 для tenant-demo

## 📊 Rate Limiting

- [ ] `/feedback` — 10 запросов → HTTP 429 (проверено: ✅)
- [ ] `/admin/invite` — 20 запросов → HTTP 429
- [ ] `/submit` — 30 запросов → HTTP 429
- [ ] Тело ответа 429 содержит описание ошибки

## 🗄️ База данных

- [ ] SQLite WAL-режим включён (PRAGMA journal_mode=WAL)
- [ ] Foreign keys включены (PRAGMA foreign_keys=ON)
- [ ] mmap_size=32MB, cache_size=-8000, synchronous=NORMAL
- [ ] SQLite → PostgreSQL миграция: DATABASE_URL env var поддержан (P1-6)
- [ ] TimescaleDB hypertables готовы (схема `trading_signals`)

## 📦 Деплой

- [ ] `pip install -r requirements.txt` (или `uv sync`)
- [ ] `python -m py_compile main.py db.py` — без ошибок
- [ ] `git tag v1.2.0` — тег выпуска
- [ ] GitHub Actions CI — все workflow зелёные
- [ ] Docker build: `docker build -t roma:v1.2.0 .`
- [ ] K8s манифесты: `deploy/manifests/`, `charts/`
- [ ] Helm: `helm install roma ./charts/roma-execution-bridge`

## 🔙 Rollback-план

1. `git checkout v1.1.0` — откат кода
2. `update_user_service svc_r5w3dVvwVPI` — перезапуск
3. Проверить `GET /health` + `/metrics` + `/dashboard`
4. Если проблема в БД — `git checkout data/roma.db` (из backup)

## 📧 Email-рассылка

- [ ] `SENDGRID_API_KEY` задан → реальная отправка
- [ ] `FROM_EMAIL` подтверждён в SendGrid
- [ ] `POST /admin/invite {"limit":3, "dry_run":true}` — логи
- [ ] `POST /admin/invite {"limit":3, "dry_run":false}` — реальные письма (1 тестовое)
- [ ] `GET /admin/email-stats` — статистика

## 🤖 Бэкенды (Slurm, Ray, Workers)

- [ ] `SLURM_ENABLED=false` → graceful 503
- [ ] `RAY_ENABLED=false` → graceful 503
- [ ] `WORKER_WS_ENABLED=false` → graceful 503
- [ ] При включении — соответствующие модули работают без ошибок импорта

---

## ✅ Финальный вердикт

- [ ] Все секреты заданы
- [ ] Health + Metrics отвечают
- [ ] Платёж создаётся (или корректный dry-run)
- [ ] Rate limiting работает
- [ ] Admin доступен только для tenant-demo
- [ ] Документация актуальна
- [ ] APM/алерты настроены
- [ ] Тег v1.2.0 создан

**Готов к production:** ▢
