# Quickstart — первая задача за 5–10 минут

## Предварительные требования

- Терминал с `curl`
- API-ключ (тестовый: `roma-demo-key-2026`)

---

## Шаг 1: Получить API-ключ

На этапе Фазы 0 используйте тестовый ключ:

```
roma-demo-key-2026
```

Все ключи хранятся в `config/api_keys.json`.

---

## Шаг 2: Проверить /health

```bash
curl https://roma-execution-bridge-asurdev.zocomputer.io/health
```

**Ожидаемый ответ:**
```json
{"status":"ok","queue_depth":0,"jobs":0}
```

Если `"status": "ok"` — сервис работает.

---

## Шаг 3: Отправить первую задачу

```bash
curl -X POST https://roma-execution-bridge-asurdev.zocomputer.io/submit \
  -H "X-API-Key: roma-demo-key-2026" \
  -H "Content-Type: application/json" \
  -d '{"task": "Train BERT classifier on GPU", "gpu_required": true, "priority": 8}'
```

**Ожидаемый ответ (202):**
```json
{
  "status": "queued",
  "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "roma_dispatch": {"protocol": "rom", "target": "rom://local/..."},
  "dag": ["validate", "dispatch", "execute", "commit"],
  "estimated_resources": {"cpu_cores": 2, "memory_mb": 512, "gpu": 0},
  "gpu_required": true
}
```

Скопируйте `job_id` — он понадобится для проверки статуса.

---

## Шаг 4: Проверить статус задачи

```bash
curl -H "X-API-Key: roma-demo-key-2026" \
  https://roma-execution-bridge-asurdev.zocomputer.io/status/ВАШ_JOB_ID
```

**Ожидаемый ответ (200):**
```json
{
  "job_id": "a1b2c3d4-...",
  "status": "queued",
  "created_at": "2026-08-12T07:48:00"
}
```

---

## Шаг 5: Посмотреть метрики

```bash
curl https://roma-execution-bridge-asurdev.zocomputer.io/metrics
```

Вы увидите Prometheus-метрики: `roma_jobs_total`, `roma_queue_depth`, `roma_requests_total` и др.

---

## Шаг 6: Посмотреть все задачи

```bash
curl -H "X-API-Key: roma-demo-key-2026" \
  https://roma-execution-bridge-asurdev.zocomputer.io/jobs
```

---

## Частые ошибки

| Ошибка | Причина | Решение |
|--------|---------|---------|
| 401 Unauthorized | Нет заголовка `X-API-Key` | Добавьте `-H "X-API-Key: roma-demo-key-2026"` |
| 401 Invalid API key | Неверный ключ | Проверьте ключ в `config/api_keys.json` |
| 404 Not Found | Неверный `job_id` | Скопируйте `job_id` из ответа `/submit` |
| 500 Internal Error | Ошибка на сервере | Проверьте логи в `/dev/shm/roma-execution-bridge*.log` |

---

## Дальнейшие шаги

- [API Reference](./api-reference.md) — полный список эндпоинтов
- [Authentication](./authentication.md) — подробно про API-ключи
- [Metrics](./metrics.md) — метрики и логирование
