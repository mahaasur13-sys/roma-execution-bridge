# ROMA Production Monitoring Plan

**Status:** Active (2026-08-15)  
**Scope:** PostgreSQL + Connection Pool + ROMA API

---

## 1. Connection Pool Metrics

| Metric | Source | PromQL | Alert Threshold |
|--------|--------|--------|:---:|
| Active connections | `pg_stat_activity` | `count(state='active')` | > 80% max_connections |
| Idle connections | `pg_stat_activity` | `count(state='idle')` | > 50 (leak signal) |
| Idle in transaction | `pg_stat_activity` | `count(state='idle in transaction')` | > 5 (> 10m) |
| Pool wait time | app logs | `ROMA_PG_POOL_WAIT_MS` gauge | > 500ms p95 |
| Pool exhausted events | app logs | `ROMA_PG_POOL_EXHAUSTED` counter | > 0 in 5m |
| Connection age | `pg_stat_activity` | `max(now() - backend_start)` | > 24h |

**Grafana panel:** `prometheus` + `postgres_exporter`

```promql
# Active connections gauge
sum by (state) (pg_stat_activity_count{datname="roma"})

# Pool utilization %
100 * sum(pg_stat_activity_count{datname="roma", state="active"}) / pg_settings_max_connections{name="max_connections"}
```

---

## 2. Database Size & Growth

| Metric | PromQL | Alert |
|--------|--------|:---:|
| DB size | `pg_database_size_bytes{datname="roma"}` | > 10 GB |
| Table sizes | `pg_stat_user_tables_size_bytes` | > 5 GB per table |
| Growth rate | `rate(pg_database_size_bytes[24h])` | > 500 MB/day |
| WAL size | `rate(pg_stat_bgwriter_buffers_clean_total[5m])` | sustained > 100/s |

```promql
# Weekly growth projection
predict_linear(pg_database_size_bytes{datname="roma"}[7d], 7*86400)

# Largest tables (top 10)
topk(10, pg_stat_user_tables_size_bytes)
```

---

## 3. Dead Tuples & Vacuum Health

| Metric | PromQL | Alert |
|--------|--------|:---:|
| Dead tuples (user tables) | `pg_stat_user_tables_n_dead_tup{schemaname!~"_timescaledb_.*"} > 1000` | `for: 15m` |
| Dead tuple ratio | `(n_dead_tup / n_live_tup) > 5` | `for: 30m` |
| Last autovacuum age | `time() - pg_stat_user_tables_last_autovacuum` | > 24h |
| Autovacuum running | `pg_stat_progress_vacuum_count` | = 0 for > 1h |
| Transaction ID age | `pg_stat_database_xact_age > 2000000000` | critical |

**Active alert:** `PostgreSQLHighDeadTuples (FIXED)` in Grafana — excludes TimescaleDB schemas, threshold `> 1000`, `for: 15m`.

---

## 4. Index Health

| Metric | PromQL | Action |
|--------|--------|--------|
| Unused indexes | `pg_stat_user_indexes_idx_scan = 0 AND idx_tup_read > 100` | Consider dropping |
| Index hit ratio | `pg_stat_database_blks_hit / (blks_hit + blks_read)` | Alert < 0.95 |
| Sequential scans | `rate(pg_stat_user_tables_seq_scan[5m])` | Alert on large tables |

---

## 5. Query Performance

| Metric | Source | Alert |
|--------|--------|:---:|
| Avg query duration | ROMA logs (JSON) | > 500ms p95 |
| Slow queries | `pg_stat_statements` | > 1s per query |
| Lock wait time | `pg_locks` + `pg_stat_activity` | > 5s |
| Deadlocks | `pg_stat_database_deadlocks` | > 0 in 5m |

```promql
# Query latency from ROMA JSON logs (via Loki)
{filename="/dev/shm/roma-execution-bridge.log"} 
  | json 
  | duration_ms > 500
```

---

## 6. Backup Health

| Check | Schedule | Alert |
|-------|----------|:---:|
| Backup file exists | Every 6h | Missing > 26h |
| Backup size > 1KB | Every backup | Size = 0 or not growing |
| Restore dry-run | Weekly | Any failure |
| Rotation working | Daily | Backups > 7 days old |

**Current:** `backup_daemon.sh` runs daily at 02:00 UTC → `/backup/postgres/roma_*.sql.gz`, 7-day rotation.

---

## 7. ROMA API Health

| Metric | Endpoint | Alert |
|--------|----------|:---:|
| API uptime | `GET /health` | Failure > 2m |
| Queue depth | `GET /health` → `queue_depth` | > 100 |
| Job failure rate | `rate(ROMA_JOB_FAILURES[5m])` | > 5% |
| Webhook latency | `POST /webhooks/cloudpayments` | p95 > 2s |
| Billing status | `GET /health` → `billing.cloudpayments_enabled` | false in production |

---

## 8. Alert Routing

| Severity | Channel | Example |
|----------|---------|---------|
| **Critical** | Telegram + email | API down, DB unreachable, backup failed 2 days |
| **Warning** | Telegram | Dead tuples > 1000, pool near exhaustion, slow queries |
| **Info** | Dashboard only | DB size approaching 80%, minor growth |

---

## Quick Start

```bash
# Check all PostgreSQL metrics
curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=pg_up'

# View backup status
ls -lh /backup/postgres/ && tail /dev/shm/backup_daemon.log

# Pool status
PGPASSWORD=postgres psql -h localhost -U postgres -d roma \
  -c "SELECT state, count(*) FROM pg_stat_activity WHERE datname='roma' GROUP BY state;"

# Large table sizes
PGPASSWORD=postgres psql -h localhost -U postgres -d roma \
  -c "SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 10;"
```

## GPU Monitoring (DCGM)

**Активация:** `./deploy/scripts/deploy_dcgm.sh`

**Ключевые метрики:**
- `DCGM_FI_DEV_GPU_UTIL` — утилизация GPU (%)
- `DCGM_FI_DEV_FB_USED` — используемая framebuffer memory
- `DCGM_FI_DEV_FB_FREE` — свободная память
- `DCGM_FI_DEV_MEM_CLOCK` — частота памяти
- `DCGM_FI_DEV_POWER_USAGE` — энергопотребление

**Дашборд:** `deploy/monitoring/grafana/grafana-gpu-dashboard.json` → импорт в Grafana.

**Алерты (рекомендуемые):**
- `DCGM_FI_DEV_GPU_UTIL > 95%` for 10m → GPU saturation
- `DCGM_FI_DEV_FB_FREE < 2GB` for 5m → low VRAM
- `DCGM_FI_DEV_POWER_USAGE > 250W` for 5m → thermal risk

## Billing (CloudPayments)

**Активация:** `./deploy/scripts/activate_cloudpayments.sh`

**Ключевые метрики:**
- `/health` → `billing.cloudpayments_enabled`
- `processed_invoices` rows per hour (Prometheus counter)
- Webhook response time (p95 < 500ms)
- Webhook error rate (< 1%)

**Алерты:**
- `billing.cloudpayments_enabled == false` for 5m → billing down
- Webhook 4xx/5xx rate > 5% → payment processing degraded

## Действия при сбоях

### Падение PostgreSQL
```bash
# 1. Проверить статус
pg_isready -h localhost
supervisorctl status roma-execution-bridge

# 2. Перезапустить PostgreSQL
pg_ctlcluster 15 main restart  # или: systemctl restart postgresql

# 3. Перезапустить ROMA
supervisorctl restart roma-execution-bridge

# 4. Проверить health
curl http://localhost:8900/health
```

### Восстановление из бэкапа
```bash
# 1. Остановить ROMA
supervisorctl stop roma-execution-bridge

# 2. Найти последний бэкап
ls -t /backup/postgres/roma_*.sql.gz | head -1

# 3. Восстановить
gunzip -c /backup/postgres/roma_2026-08-15_070022.sql.gz | \
  PGPASSWORD=postgres psql -h localhost -U postgres -d roma

# 4. Запустить ROMA
supervisorctl start roma-execution-bridge
```

### Перезапуск всех сервисов
```bash
supervisorctl restart roma-execution-bridge astrofin-api astrofin-grafana evolution-worker backup-daemon
```

### Проверка целостности после восстановления
```bash
# Проверить количество таблиц
PGPASSWORD=postgres psql -h localhost -U postgres -d roma -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';"

# Проверить ключевые функции
cd /home/workspace/roma-execution-bridge && python3 -c "
import os; os.environ['PG_DSN']='postgresql://postgres:postgres@localhost:5432/roma'
import db_adapter as db
print('is_invoice_processed:', db.is_invoice_processed('test-123'))
print('add_lead:', db.add_lead('recovery-test@example.com','RecoveryCo','QA'))
"
```
