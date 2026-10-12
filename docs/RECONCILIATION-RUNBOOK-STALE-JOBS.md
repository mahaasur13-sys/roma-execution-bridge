# Reconciliation Runbook — Stale Jobs (RaaS-05)

**Статус:** ПОДГОТОВЛЕН. **Не исполнять** без отдельного Owner GO.
**Режим:** READ-ONLY. Запрещено менять `execution_jobs`, запускать/отменять jobs, звать provider API, делать UPDATE/DELETE/retry.

---

## 1. Источник истины

- **Primary:** `execution_jobs` (PostgreSQL — `migrations/002_billing_pg.sql` + `007_execution_billing.sql`).
  Колонки: `id`, `decision_id`, `tenant_id`, `status`, `payload`, `created_at`, `updated_at`,
  `backend`, `backend_job_id`, `duration_seconds`, `started_at`, `completed_at`, `error`, `cost_usd`.
- **Read-only доступ (штатный):** `db_adapter.list_jobs(tenant_id, limit)`, `get_execution_job(job_id)`,
  `count_jobs_for_tenant_total(tenant_id)`, `count_jobs_active_for_tenant(tenant_id)`.
- **In-memory control-plane (отдельный контур):** `control_plane/job_store.py` (JobStore),
  `control_plane/leases.py` (GPULeaseManager), `control_plane/reconciler.py` (Reconciler).

## 2. Категории статусов (из кода)

- **Terminal:** `completed`, `failed`, `cancelled`, `timeout`.
- **Active/in-flight:** `pending`, `queued`, `running`.
- **Billing edge:** `billing_pending` (job выполнен, но деньги не списаны — отдельная категория, не «completed»).
- **Managed:** `atom_cluster_managed` (кластерный submit).
- **Не удалось подтвердить (unconfirmed):** строка, чьё внешнее состояние нельзя установить read-only
  (нет `backend_job_id`, нет lease, нет свежего heartbeat). Это **отдельная** категория —
  её НЕ сворачивают в `completed`/`failed`.

## 3. Проверка внешнего instance/lease БЕЗ вызова провайдера

Только чтение:

1. `backend` и `backend_job_id` заполнены? Если `backend_job_id` пуст → внешнего хендла нет → **«не удалось подтвердить»**.
2. Worker liveness: `control_plane/registry.py` WorkerRegistry — `last_heartbeat`, `is_healthy()`.
   `running` на мёртвом worker → кандидат (см. `reconciler._tick`).
3. Lease: `control_plane/leases.py` GPULeaseManager — `is_expired()`, `cleanup_expired()`.
   `running` с истёкшим lease → кандидат.
4. **Сетевые вызовы к Vast.ai / CloudPayments / Stripe запрещены.**

## 4. Canary (1 job)

1. Выбрать ровно **один** job из одной категории.
2. Записать его `id` + состояние «до».
3. Применить единственное решение согласования.
4. Проверить результат и выждать один цикл наблюдения; только затем — следующая партия.

## 5. Контроль дублей

- Ключ согласования — `execution_jobs.id` (уникальный PK). Каждый job трогается **один раз**.
- Перед любым действием фиксировать пару `(job_id → действие)` и повторно читать текущий статус
  (optimistic re-check); job, уже согласованный другим процессом, — пропускать.

## 6. Критерии остановки

- Любое действие шире canary-партии.
- Любое требование вызвать provider API (запрещено).
- Любой job, чей tenant/владелец не подтверждён.
- Любая запись в `execution_jobs` на read-only-фазе — стоп, запросить Owner GO.

## 7. «Не удалось подтвердить»

Job, который нельзя классифицировать только по read-only данным, фиксируется как **unconfirmed**
(не `completed`, не `failed`), с указанием, каких данных не хватило (нет `backend_job_id` / lease / heartbeat).
