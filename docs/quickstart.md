# Quickstart — ROMA Execution Bridge

**Время:** 5–10 минут до первой задачи

**URL сервиса:** `https://roma-execution-bridge-asurdev.zocomputer.io`

---

## Шаг 1: Получить API-ключ

Тестовый ключ: **`roma-demo-key-2026`**

Все доступные ключи — в `config/api_keys.json`. Каждый ключ привязан к своему tenant'у (tenant-demo, tenant-alpha, tenant-bravo).

---

## Шаг 2: Проверить, что сервис жив

```bash
curl https://roma-execution-bridge-asurdev.zocomputer.io/health
```

**Ожидаемый ответ:**
```json
{"status":"ok","queue_depth":0,"jobs":0,"billing":{"stripe_enabled":false}}
```

---

## Шаг 3: Открыть дашборд

**В браузере:**

```
https://roma-execution-bridge-asurdev.zocomputer.io/dashboard?api_key=roma-demo-key-2026
```

На дашборде: аккаунт (tenant, план, лимиты), последние задачи (таблица), быстрые действия (health, metrics, usage), демо-задачи (кнопки).

---

## Шаг 4: Запустить демо-задачу (одной кнопкой)

```bash
curl -X POST https://roma-execution-bridge-asurdev.zocomputer.io/demo/demo-pytorch-train \
  -H "X-API-Key: roma-demo-key-2026"
```

**Или из браузера** — на дашборде нажать «🧠 Training» в секции Demo Tasks.

**Доступные демо:**

| Демо | Команда | GPU |
|------|---------|:---:|
| PyTorch Training | `POST /demo/demo-pytorch-train` | ✅ |
| BERT Inference | `POST /demo/demo-inference` | ✅ |
| Batch Processing | `POST /demo/demo-batch-processing` | ❌ |
| GPU Benchmark | `POST /demo/demo-gpu-benchmark` | ✅ |

**Ожидаемый ответ (202):**
```json
{
  "status": "queued",
  "job_id": "a1b2c3d4-...",
  "tenant_id": "tenant-demo",
  "dag": ["validate", "dispatch", "execute", "commit"],
  "gpu_required": true
}
```

---

## Шаг 5: Отправить свою задачу

```bash
curl -X POST https://roma-execution-bridge-asurdev.zocomputer.io/submit \
  -H "X-API-Key: roma-demo-key-2026" \
  -H "Content-Type: application/json" \
  -d '{"task": "Fine-tune BERT on custom dataset", "gpu_required": true, "priority": 8}'
```

**Параметры:**
| Поле | Тип | По умолчанию | Описание |
|------|-----|:---:|----------|
| `task` | string | — | Описание задачи (обязательное) |
| `gpu_required` | bool | `false` | Нужен ли GPU |
| `priority` | int | `5` | Приоритет 1–10 |
| `execution_mode` | string | `k8s_job` | k8s_job, batch, atom_cluster |

---

## Шаг 6: Проверить статус задачи

```bash
curl -H "X-API-Key: roma-demo-key-2026" \
  https://roma-execution-bridge-asurdev.zocomputer.io/status/ВАШ_JOB_ID
```

**Ответ (200):**
```json
{"job_id":"a1b2c3d4-...","status":"queued","created_at":"2026-08-12T..."}
```

---

## Шаг 7: Посмотреть использование

```bash
curl -H "X-API-Key: roma-demo-key-2026" \
  https://roma-execution-bridge-asurdev.zocomputer.io/usage
```

**Ответ:**
```json
{
  "tenant_id": "tenant-demo",
  "plan": "free",
  "usage": {"total_jobs": 3, "total_gpu_seconds": 600, "last_updated": "..."},
  "limits": {"max_jobs_per_month": 50, "max_jobs_per_month_display": "50"}
}
```

---

## Шаг 8: Посмотреть метрики

```bash
curl https://roma-execution-bridge-asurdev.zocomputer.io/metrics
```

Вы увидите Prometheus-метрики: `roma_jobs_total`, `roma_jobs_active`, `roma_queue_depth`, `roma_requests_total`, `roma_request_duration_seconds` — все с лейблом `tenant_id`.

---

## Частые ошибки

| Ошибка | Причина | Решение |
|--------|---------|---------|
| 401 Unauthorized | Нет заголовка `X-API-Key` | Добавьте `-H "X-API-Key: roma-demo-key-2026"` |
| 401 Invalid API key | Неверный ключ | Проверьте ключ в `config/api_keys.json` |
| 402 Payment Required | Лимит тарифа исчерпан | Апгрейдните план через `/billing/create-checkout-session` |
| 404 Job not found | Неверный `job_id` или чужой tenant | Задачи изолированы по tenant'ам |
| 404 Demo not found | Неверное имя демо | `GET /demos` покажет список |

---

## Тарифные планы

| План | Задач/мес | Цена |
|------|:---------:|------|
| Free | 50 | $0 |
| Pro | 1 000 | $49/мес |
| Enterprise | Unlimited | $299/мес |

---

## Дальнейшие шаги

- [API Reference](./api-reference.md) — полный список эндпоинтов
- [Dashboard](https://roma-execution-bridge-asurdev.zocomputer.io/dashboard?api_key=roma-demo-key-2026)
- [Демо-задачи](./demos.md)
- [Биллинг](./billing.md)
- [Метрики и логи](./metrics.md)
- [Архитектура](./architecture.md)
