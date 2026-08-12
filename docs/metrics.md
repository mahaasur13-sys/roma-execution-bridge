# Метрики и логирование

## Prometheus `/metrics`

Эндпоинт доступен публично (без API-ключа):

```bash
curl https://roma-execution-bridge-asurdev.zocomputer.io/metrics
```

Формат ответа: `text/plain` (Prometheus exposition format).

### Доступные метрики

| Метрика | Тип | Лейблы | Описание |
|---------|-----|--------|----------|
| `roma_jobs_total` | Counter | — | Общее количество отправленных задач с момента запуска |
| `roma_jobs_active` | Gauge | — | Текущее количество активных задач |
| `roma_queue_depth` | Gauge | — | Текущая глубина очереди |
| `roma_requests_total` | Counter | `endpoint`, `method`, `status` | Счётчик HTTP-запросов |
| `roma_request_duration_seconds` | Histogram | `endpoint`, `method` | Длительность запросов |

### Пример ответа

```text
# HELP roma_jobs_total Total number of submitted jobs
# TYPE roma_jobs_total counter
roma_jobs_total 3.0

# HELP roma_jobs_active Currently active jobs
# TYPE roma_jobs_active gauge
roma_jobs_active 3.0

# HELP roma_queue_depth Current queue depth
# TYPE roma_queue_depth gauge
roma_queue_depth 0.0

# HELP roma_requests_total Total HTTP requests
# TYPE roma_requests_total counter
roma_requests_total{endpoint="/health",method="GET",status="200"} 8.0
roma_requests_total{endpoint="/metrics",method="GET",status="200"} 3.0
roma_requests_total{endpoint="/submit",method="POST",status="202"} 3.0
roma_requests_total{endpoint="/submit",method="POST",status="401"} 2.0
roma_requests_total{endpoint="/jobs",method="GET",status="200"} 1.0

# HELP roma_request_duration_seconds Request duration in seconds
# TYPE roma_request_duration_seconds histogram
roma_request_duration_seconds_bucket{endpoint="/submit",method="POST",le="0.005"} 3.0
roma_request_duration_seconds_bucket{endpoint="/submit",method="POST",le="0.01"} 3.0
roma_request_duration_seconds_bucket{endpoint="/submit",method="POST",le="+Inf"} 3.0
roma_request_duration_seconds_count{endpoint="/submit",method="POST"} 3.0
roma_request_duration_seconds_sum{endpoint="/submit",method="POST"} 0.002
```

## JSON-логирование

Каждый HTTP-запрос логируется в структурированном JSON-формате.

### Формат записи

```json
{
  "timestamp": "2026-08-12T07:48:17.836134Z",
  "level": "INFO",
  "endpoint": "/submit",
  "method": "POST",
  "status_code": 202,
  "duration_ms": 0.9,
  "api_key": "roma***2026",
  "message": "POST /submit → 202"
}
```

### Поля

| Поле | Описание |
|------|----------|
| `timestamp` | ISO 8601 с миллисекундами |
| `level` | `INFO` (2xx/3xx), `WARNING` (4xx), `ERROR` (5xx) |
| `endpoint` | Путь запроса (`/submit`, `/health`, ...) |
| `method` | HTTP-метод (`GET`, `POST`) |
| `status_code` | HTTP-код ответа |
| `duration_ms` | Длительность обработки в миллисекундах |
| `api_key` | Маскированный API-ключ (`roma***2026`) или `null` |
| `message` | Человекочитаемое описание |

### Для ошибок

При `level: ERROR` добавляются поля:

```json
{
  "level": "ERROR",
  "error": "Описание ошибки",
  "traceback": ["Traceback (most recent call last):\n", "  ..."]
}
```

### Где смотреть логи

```bash
# Основной лог (stdout uvicorn + JSON middleware)
tail -f /dev/shm/roma-execution-bridge.log

# Ошибки (stderr)
tail -f /dev/shm/roma-execution-bridge_err.log

# Через Loki (фильтр по endpoint)
curl -G -s "http://localhost:3100/loki/api/v1/query_range" \
  --data-urlencode 'query={filename="/dev/shm/roma-execution-bridge.log"} | json' \
  --data-urlencode "limit=10" | jq '.data.result[0].values[][1]'
```

## Интеграция с Grafana

Метрики `/metrics` можно добавить как Prometheus datasource в Grafana:

1. Добавить scrape target в `prometheus.yml`:
   ```yaml
   - job_name: roma-execution-bridge
     static_configs:
       - targets: ['localhost:8900']
   ```
2. Импортировать готовый дашборд (будет добавлен в Фазе 1)
