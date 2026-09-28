# Аутентификация — X-API-Key

## Обзор

ROMA использует аутентификацию через заголовок `X-API-Key`. Это простой и надёжный способ для API-first сервиса на этапе Pre-Launch.

## Как это работает

1. Клиент отправляет HTTP-запрос с заголовком `X-API-Key: <ключ>`
2. FastAPI-зависимость `verify_api_key()` проверяет наличие и валидность ключа
3. Неверный/отсутствующий ключ → **401 Unauthorized**
4. Валидный ключ → запрос обрабатывается

## Хранение ключей

Ключи тенантов хранятся в PostgreSQL (`tenants`) **только в виде хеша** (`api_key_hash` = `sha256(raw_key)`). Открытый (plaintext) ключ в БД не сохраняется: колонка `tenants.api_key` остаётся пустой и не участвует в аутентификации.

- Ключ выдаётся один раз — при создании тенанта; в БД пишется только его хеш.
- Проверка запроса: `verify_api_key()` сравнивает `sha256(X-API-Key)` с `tenants.api_key_hash`.
- Plaintext-fallback и ленивый backfill удалены (G-SEC4): ключ, у которого нет хеша, не аутентифицируется.

## Связь ключа и Tenant

Каждый ключ сопоставляется с `tenant_id` через таблицу `tenants` (по `api_key_hash`), а не через файл.

**Tenant isolation:**
- Каждый tenant видит только свои задачи
- Попытка доступа к чужой задаче → **404** (не 403 — чужой job_id не раскрывается)
- `/jobs` возвращает только задачи текущего tenant

**Добавление нового tenant'а:**

```bash
# 1. Сгенерировать ключ
python3 -c "import uuid; print('roma-key-' + uuid.uuid4().hex[:12])"

# 2. Записать в БД только sha256(ключа) в tenants.api_key_hash
# 3. Ключ выдать тенанту один раз (plaintext в БД не сохранять)
```

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
  -H "X-API-Key: <your_api_key>" \
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

- API-ключи **маскируются** в логах (первые/последние символы)
- Хранилище ключей — PostgreSQL `tenants.api_key_hash` (sha256): plaintext отсутствует и в БД, и в ответах

## Добавление нового ключа

Ключ создаётся при онбординге тенанта; в БД записывается только `sha256(ключа)`.
`config/api_keys.json` — устаревший артефакт, кодом не читается, источником истины не является.

## OAuth2 (Google / GitHub)

ROMA поддерживает вход через Google и GitHub (опционально). Для включения задайте переменные окружения:

```env
GOOGLE_CLIENT_ID=xxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxx
GITHUB_CLIENT_ID=Ov23li...
GITHUB_CLIENT_SECRET=...
```

После настройки на странице входа (`/auth/login`) появятся кнопки «Войти через Google» и «Войти через GitHub».

При первом входе создаётся новый tenant, генерируется API-ключ для машинного доступа.

**Callback URL (настройка в Google Cloud Console / GitHub OAuth App):**
`https://roma-execution-bridge-asurdev.zocomputer.io/auth/oauth/callback/google`
