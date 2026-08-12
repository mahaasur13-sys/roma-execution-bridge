# ROMA Execution Bridge — Документация

**ROMA** — SaaS-платформа для закрытого цикла выполнения GPU/CPU-задач: от отправки до биллинга.

Сервис запущен: `https://roma-execution-bridge-asurdev.zocomputer.io`

## Навигация

| Документ | Описание |
|----------|----------|
| [quickstart.md](./quickstart.md) | Запуск первой задачи за 5–10 минут |
| [api-reference.md](./api-reference.md) | Полный API-справочник (все эндпоинты) |
| [authentication.md](./authentication.md) | Работа с API-ключами, ошибки 401 |
| [architecture.md](./architecture.md) | Архитектура сервиса и компоненты |
| [metrics.md](./metrics.md) | Prometheus-метрики и JSON-логирование |
| [phase-0-pre-launch.md](./phase-0-pre-launch.md) | План развития: Фаза 0 (Pre-Launch, 0–2 мес) |
| [competitive-analysis.md](./competitive-analysis.md) | Конкурентный анализ: TCO, Time-to-Value, риски |
| [WHITE-LABEL-QUICKSTART.md](./WHITE-LABEL-QUICKSTART.md) | White-label / брендирование SaaS |

## Быстрый старт

```bash
# 1. Проверить, что сервис жив
curl https://roma-execution-bridge-asurdev.zocomputer.io/health

# 2. Отправить задачу (нужен ключ)
curl -X POST https://roma-execution-bridge-asurdev.zocomputer.io/submit \
  -H "X-API-Key: roma-demo-key-2026" \
  -H "Content-Type: application/json" \
  -d '{"task": "Train sentiment model on GPU", "gpu_required": true}'
```

Подробнее → [quickstart.md](./quickstart.md)

## Текущая версия

**v1.0.0** — Фаза 0 (Pre-Launch). Готовимся к закрытому бета-тесту.
