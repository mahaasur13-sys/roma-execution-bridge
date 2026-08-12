# Аутентификация — X-API-Key

## Обзор

ROMA использует аутентификацию через заголовок `X-API-Key`. Это простой и надёжный способ для API-first сервиса на этапе Pre-Launch.

## Как это работает

1. Клиент отправляет HTTP-запрос с заголовком `X-API-Key: <ключ>`
2. FastAPI-зависимость `verify_api_key()` проверяет наличие и валидность ключа
3. Неверный/отсутствующий ключ → **401 Unauthorized**
4. Валидный ключ → запрос обрабатывается

## Тестовые ключи

На время Фазы 0 используются тестовые ключи из `config/api_keys.json`:

| Ключ | Назначение |
|------|------------|
| `roma-demo-key-2026` | Основной демо-ключ |
| `roma-test-key-alpha` | Для тестирования |
| `roma-test-key-bravo` | Для тестирования |

## Публичные эндпоинты (без ключа)

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/health` | Статус сервиса |
| GET | `/metrics` | Prometheus-метрики |

## Защищённые эндпоинты (требуют ключ)

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/submit` | Отправить задачу |
| GET | `/status/{job_id}` | Статус задачи |
| POST | `/cancel/{job_id}` | Отменить задачу |
| GET | `/jobs` | Список задач |
| POST | `/submit/cluster` | Задача через ATOMCluster |

## Примеры

### Без ключа → 401

```bash
curl -X POST https://roma-execution-bridge-asurdev.zocomputer.io/submit \
  -H "Content-Type: application/json" \
  -d '{"task": "test"}'
```

**Ответ:**
```
HTTP 401
{"detail": "Missing X-API-Key header. Request a key at https://roma-execution-bridge-asurdev.zocomputer.io"}
```

### Неверный ключ → 401

```bash
curl -X POST https://roma-execution-bridge-asurdev.zocomputer.io/submit \
  -H "X-API-Key: wrong-key" \
  -H "Content-Type: application/json" \
  -d '{"task": "test"}'
```

**Ответ:**
```
HTTP 401
{"detail": "Invalid API key"}
```

### Верный ключ → 202

```bash
curl -X POST https://roma-execution-bridge-asurdev.zocomputer.io/submit \
  -H "X-API-Key: roma-demo-key-2026" \
  -H "Content-Type: application/json" \
  -d '{"task": "Train model"}'
```

**Ответ:**
```
HTTP 202
{"status": "queued", "job_id": "...", ...}
```

## Коды ошибок

| Код | Сообщение | Причина |
|-----|-----------|---------|
| 401 | `Missing X-API-Key header...` | Заголовок отсутствует |
| 401 | `Invalid API key` | Ключ не найден в списке |

## Безопасность

- API-ключи **маскируются** в логах: `roma-demo-key-2026` → `roma***2026`
- Файл `config/api_keys.json` не включается в публичные ответы
- В будущем ключи будут храниться в PostgreSQL с хешированием

## Добавление нового ключа

Отредактируйте `config/api_keys.json`:

```json
{
  "keys": [
    "roma-demo-key-2026",
    "roma-test-key-alpha",
    "roma-test-key-bravo",
    "ваш-новый-ключ"
  ]
}
```

Перезапустите сервис:

```bash
# Сервис перезапускается автоматически при обновлении через update_user_service
```
