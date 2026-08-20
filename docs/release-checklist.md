# ROMA Production Release Checklist

> Чек-лист для перехода из staging в production.
> Пройти все пункты перед развёртыванием на live-кластере.

## 1. GPU Infrastructure

- [ ] GPU-ноды добавлены в K8s-кластер (`kubectl get nodes -l nvidia.com/gpu=true`)
- [ ] GPU-драйверы и NVIDIA Container Toolkit установлены
- [ ] DCGM Exporter развёрнут: `./deploy/scripts/deploy_dcgm.sh`
- [ ] Метрики GPU идут в Prometheus: `DCGM_FI_DEV_GPU_UTIL`, `DCGM_FI_DEV_FB_USED`
- [ ] GPU-дашборд импортирован в Grafana: `deploy/monitoring/grafana/grafana-gpu-dashboard.json`

## 2. Billing (CloudPayments)

- [ ] Live-ключи получены от CloudPayments (Public ID, API Secret, Webhook Secret)
- [ ] Ключи внесены в `.env`: `CLOUDPAYMENTS_PUBLIC_ID`, `CLOUDPAYMENTS_API_SECRET`, `CLOUDPAYMENTS_WEBHOOK_SECRET`
- [ ] `CLOUDPAYMENTS_MODE=live`
- [ ] Активация выполнена: `./deploy/scripts/activate_cloudpayments.sh --test`
- [ ] Тестовая транзакция прошла: `is_invoice_processed()` → `True`
- [ ] Webhook URL настроен в CloudPayments Dashboard

## 3. PostgreSQL

- [ ] `PG_DSN` задан в окружении ROMA API
- [ ] `ThreadedConnectionPool` активен (min=2, max=20)
- [ ] Keepalive-параметры применены
- [ ] Бэкапы настроены: ежедневно в 02:00 UTC, ротация 7 дней
- [ ] `VACUUM ANALYZE` запускается по cron (03:00 UTC)
- [ ] `pg_stat_activity` показывает стабильные соединения (нет утечек)

## 4. Alerting & Notifications

- [ ] PostgreSQLHighDeadTuples alert активен (Grafana, id=8)
- [ ] PostgreSQl Critical Dead Tuples alert активен (id=9)
- [ ] Telegram contact point настроен и протестирован
- [ ] Notification policy привязана к алертам
- [ ] Тестовое уведомление отправлено и получено

## 5. Smoke Testing

- [ ] `curl http://localhost:8900/health` → `{"status":"ok"}`
- [ ] GPU-статус в `/health`: `gpu_available` должен быть `true` на GPU-нодах
- [ ] `is_invoice_processed()` / `mark_invoice_processed()` — PASS
- [ ] `add_lead()` — PASS
- [ ] Параллельные запросы (20 шт) — 0 ошибок
- [ ] `curl http://localhost:8900/stats/daily` → 200 OK
- [ ] Webhook `/webhooks/cloudpayments` — обрабатывает запросы без 500

## 6. Documentation

- [ ] `docs/AIOPS-STATUS-2026-08-15.md` — сводный статус-репорт
- [ ] `docs/production-monitoring.md` — план мониторинга (обновлён GPU + billing)
- [ ] `docs/DCGM-deployment.md` — инструкция развёртывания GPU-мониторинга
- [ ] `docs/CloudPayments-setup.md` — инструкция активации биллинга
- [ ] `AGENTS.md` — обновлён с ключевыми изменениями

## 7. Pre-Launch Sanity

- [ ] Supervisor: все программы RUNNING
- [ ] Grafana: все дашборды загружены, алерты активны
- [ ] Prometheus: таргеты UP, метрики текут
- [ ] Нет критических ошибок в логах за последние 24 часа
