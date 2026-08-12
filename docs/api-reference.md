# ROMA Execution Bridge — API Reference

## Base URL

```
https://roma-execution-bridge-asurdev.zocomputer.io
```

## Authentication

Все защищённые эндпоинты требуют заголовок:

```
X-API-Key: <your-api-key>
```

Без ключа или с неверным ключом → **401 Unauthorized**.

### Получение ключа

Тестовые ключи хранятся в `config/api_keys.json`. Для production — запрос ключа через лендинг (доступен в Фазе 0, неделя 5–6).

### Пример запроса

```bash
# Правильный запрос (200/202)
curl -X POST https://roma-execution-bridge-asurdev.zocomputer.io/submit \
  -H "Content-Type: application/json" \
  -H "X-API-Key: roma-demo-key-2026" \
  -d '{"task": "train PyTorch model", "gpu_required": true}'

# Без ключа (401)
curl https://roma-execution-bridge-asurdev.zocomputer.io/jobs
# → {"detail": "Missing X-API-Key header. Request a key at ..."}

# Неверный ключ (401)
curl https://roma-execution-bridge-asurdev.zocomputer.io/jobs \
  -H "X-API-Key: wrong-key"
# → {"detail": "Invalid API key"}
```

---

## Endpoints

### GET /health

**Публичный (без ключа).**

```bash
curl https://roma-execution-bridge-asurdev.zocomputer.io/health
```

**Response 200:**
```json
{
  "status": "ok",
  "queue_depth": 0,
  "jobs": 1
}
```

---

### POST /submit

**Требует ключ.** Отправить задачу на выполнение.

```bash
curl -X POST https://roma-execution-bridge-asurdev.zocomputer.io/submit \
  -H "Content-Type: application/json" \
  -H "X-API-Key: roma-demo-key-2026" \
  -d '{
    "task": "train PyTorch model on 2 GPUs",
    "gpu_required": true,
    "priority": 8,
    "execution_mode": "k8s_job"
  }'
```

**Request body:**

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|:------------:|--------------|----------|
| `task` | string | ✅ | — | Описание задачи (min 1 символ) |
| `gpu_required` | bool | | `false` | Нужен ли GPU |
| `priority` | int | | `5` | Приоритет 1–10 |
| `execution_mode` | str | | `k8s_job` | Режим: `k8s_job`, `k8s_persistent`, `atom_cluster`, `batch` |

**Response 202:**
```json
{
  "status": "queued",
  "job_id": "24740fcd-63bd-462a-bf58-886d78764857",
  "roma_dispatch": {
    "protocol": "rom",
    "target": "rom://local/24740fcd-63bd-462a-bf58-886d78764857"
  },
  "dag": ["validate", "dispatch", "execute", "commit"],
  "estimated_resources": {
    "cpu_cores": 2,
    "memory_mb": 512,
    "gpu": 0
  },
  "gpu_required": false
}
```

---

### GET /status/{job_id}

**Требует ключ.** Проверить статус задачи.

```bash
curl https://roma-execution-bridge-asurdev.zocomputer.io/status/24740fcd-63bd-462a-bf58-886d78764857 \
  -H "X-API-Key: roma-demo-key-2026"
```

**Response 200:**
```json
{
  "job_id": "24740fcd-63bd-462a-bf58-886d78764857",
  "status": "queued",
  "created_at": "2026-08-12T07:45:04",
  "started_at": null,
  "completed_at": null,
  "error": null
}
```

**Response 404:**
```json
{
  "detail": "Job not found"
}
```

---

### POST /cancel/{job_id}

**Требует ключ.** Отменить задачу.

```bash
curl -X POST https://roma-execution-bridge-asurdev.zocomputer.io/cancel/24740fcd-63bd-462a-bf58-886d78764857 \
  -H "X-API-Key: roma-demo-key-2026"
```

**Response 200:**
```json
{
  "status": "cancelled",
  "job_id": "24740fcd-63bd-462a-bf58-886d78764857"
}
```

---

### GET /jobs

**Требует ключ.** Список последних 10 задач.

```bash
curl https://roma-execution-bridge-asurdev.zocomputer.io/jobs \
  -H "X-API-Key: roma-demo-key-2026"
```

**Response 200:**
```json
{
  "rom_version": "1.0.0",
  "queue": 1,
  "jobs": [
    {
      "status": "queued",
      "job_id": "24740fcd-63bd-462a-bf58-886d78764857",
      "rom": "rom://local/24740fcd-63bd-462a-bf58-886d78764857",
      "submitted_at": "2026-08-12T07:45:04",
      "payload": {
        "task": "test",
        "gpu_required": false,
        "priority": 5,
        "execution_mode": "k8s_job"
      }
    }
  ],
  "execution_modes": ["k8s_job", "k8s_persistent", "atom_cluster", "batch"]
}
```

---

### POST /submit/cluster

**Требует ключ.** Отправить задачу как ATOMCluster.

```bash
curl -X POST https://roma-execution-bridge-asurdev.zocomputer.io/submit/cluster \
  -H "Content-Type: application/json" \
  -H "X-API-Key: roma-demo-key-2026" \
  -d '{"cluster_spec": {"name": "my-cluster", "nodes": 4}}'
```

---

## Коды ошибок

| Код | Описание |
|-----|----------|
| 200 | OK — запрос выполнен |
| 202 | Accepted — задача принята в очередь |
| 401 | Unauthorized — отсутствует или неверный `X-API-Key` |
| 404 | Not Found — задача с указанным `job_id` не найдена |
| 500 | Internal Server Error |

---

## Тестовые ключи

Актуальные ключи в `config/api_keys.json`:

- `roma-demo-key-2026`
- `roma-test-key-alpha`
- `roma-test-key-bravo`

> Для production: ключи выдаются через лендинг и хранятся в `ROMA_API_KEYS` (переменная окружения).
