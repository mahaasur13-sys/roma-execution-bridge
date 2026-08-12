# ROMA Execution Bridge — API Reference

## Base URL

```
https://roma-execution-bridge-asurdev.zocomputer.io
```

---

## Authentication

Защищённые эндпоинты требуют заголовок:

```
X-API-Key: <your_api_key>
```

Тестовые ключи доступны в `config/api_keys.json`.

### Поведение

| Сценарий | Код | Ответ |
|----------|-----|-------|
| Ключ отсутствует | 401 | `{"detail": "Missing X-API-Key header..."}` |
| Ключ неверный | 401 | `{"detail": "Invalid API key"}` |
| Ключ верный | 2xx | Данные |

---

## Public Endpoints

### GET /health

Статус сервиса. **Не требует аутентификации.**

```bash
curl https://roma-execution-bridge-asurdev.zocomputer.io/health
```

**Response (200):**
```json
{
  "status": "ok",
  "queue_depth": 0,
  "jobs": 0
}
```

---

### GET /metrics

Prometheus-метрики в формате `text/plain`. **Не требует аутентификации.**

```bash
curl https://roma-execution-bridge-asurdev.zocomputer.io/metrics
```

**Response (200, text/plain):**
```text
# HELP roma_jobs_total Total number of submitted jobs
# TYPE roma_jobs_total counter
roma_jobs_total 2.0
# HELP roma_jobs_active Currently active jobs
# TYPE roma_jobs_active gauge
roma_jobs_active 2.0
# HELP roma_queue_depth Current queue depth
# TYPE roma_queue_depth gauge
roma_queue_depth 0.0
# HELP roma_requests_total Total HTTP requests
# TYPE roma_requests_total counter
roma_requests_total{endpoint="/health",method="GET",status="200"} 5.0
roma_requests_total{endpoint="/metrics",method="GET",status="200"} 3.0
roma_requests_total{endpoint="/submit",method="POST",status="202"} 2.0
# HELP roma_request_duration_seconds Request duration in seconds
# TYPE roma_request_duration_seconds histogram
roma_request_duration_seconds_bucket{endpoint="/submit",method="POST",le="0.005"} 2.0
...
```

**Доступные метрики:**

| Метрика | Тип | Описание |
|---------|-----|----------|
| `roma_jobs_total` | Counter | Общее количество отправленных задач |
| `roma_jobs_active` | Gauge | Текущие активные задачи |
| `roma_queue_depth` | Gauge | Глубина очереди |
| `roma_requests_total` | Counter | Счётчик HTTP-запросов (labels: endpoint, method, status) |
| `roma_request_duration_seconds` | Histogram | Длительность запросов (labels: endpoint, method) |

---

## Protected Endpoints

### POST /submit

Отправить задачу на выполнение.

```bash
curl -X POST https://roma-execution-bridge-asurdev.zocomputer.io/submit \
  -H "X-API-Key: roma-demo-key-2026" \
  -H "Content-Type: application/json" \
  -d '{"task": "Train Bert on GPU", "gpu_required": true, "priority": 8}'
```

**Request Body:**
```json
{
  "task": "string (required)",
  "gpu_required": false,
  "priority": 5,
  "execution_mode": "k8s_job"
}
```

**Response (202):**
```json
{
  "status": "queued",
  "job_id": "a1b2c3d4-...",
  "roma_dispatch": {"protocol": "rom", "target": "rom://local/..."},
  "dag": ["validate", "dispatch", "execute", "commit"],
  "estimated_resources": {"cpu_cores": 2, "memory_mb": 512, "gpu": 0},
  "gpu_required": true
}
```

---

### GET /status/{job_id}

Получить статус задачи.

```bash
curl -H "X-API-Key: roma-demo-key-2026" \
  https://roma-execution-bridge-asurdev.zocomputer.io/status/a1b2c3d4-...
```

**Response (200):**
```json
{
  "job_id": "a1b2c3d4-...",
  "status": "queued",
  "created_at": "2026-08-12T07:48:00",
  "started_at": null,
  "completed_at": null,
  "error": null
}
```

**Response (404):**
```json
{"detail": "Job not found"}
```

---

### POST /cancel/{job_id}

Отменить задачу.

```bash
curl -X POST -H "X-API-Key: roma-demo-key-2026" \
  https://roma-execution-bridge-asurdev.zocomputer.io/cancel/a1b2c3d4-...
```

**Response (200):**
```json
{"status": "cancelled", "job_id": "a1b2c3d4-..."}
```

---

### GET /jobs

Список всех задач.

```bash
curl -H "X-API-Key: roma-demo-key-2026" \
  https://roma-execution-bridge-asurdev.zocomputer.io/jobs
```

**Response (200):**
```json
{
  "rom_version": "1.0.0",
  "queue": 2,
  "jobs": [...],
  "execution_modes": ["k8s_job", "k8s_persistent", "atom_cluster", "batch"]
}
```

---

### POST /submit/cluster

Отправить задачу как ATOMCluster.

```bash
curl -X POST https://roma-execution-bridge-asurdev.zocomputer.io/submit/cluster \
  -H "X-API-Key: roma-demo-key-2026" \
  -H "Content-Type: application/json" \
  -d '{"cluster_spec": {"name": "demo-cluster", "nodes": 4}}'
```

**Response (202):**
```json
{
  "status": "atom_cluster_managed",
  "job_id": "...",
  "cluster_name": "demo-cluster",
  "execution_mode": "atom_cluster",
  "atom_cluster": {"name": "demo-cluster", "managed": true, "nodes": 4}
}
```
