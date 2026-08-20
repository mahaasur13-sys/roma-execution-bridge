# ROMA Execution Bridge — Beta Playbook

## Обзор

Закрытая бета ROMA Execution Bridge v2.1.0. Цель: 10–20 активных бета-тестеров (ML-инженеры, DevOps, исследователи).

## Подготовка (1 день)

### 1. Активировать beta-режим

```bash
export BETA_MODE=true
export BETA_MAX_USERS=20          # максимум пользователей
export BETA_REQUIRE_INVITE=true   # только по инвайтам
export BETA_DEFAULT_SPEND_CAP_USD=5.00
```

Перезапустить сервис: `update_user_service svc_r5w3dVvwVPI`

### 2. Сгенерировать инвайт-коды

```bash
# Через админ-эндпоинт
curl -X POST /admin/invites/create \
  -H "X-API-Key: admin-key-beta-2026" \
  -H "Content-Type: application/json" \
  -d '{"description":"Batch 1","max_uses":1,"tenant_plan":"free"}'

# Просмотр всех инвайтов
curl /admin/invites -H "X-API-Key: admin-key-beta-2026"
```

### 3. Подготовить пригласительные письма

Шаблон: `saas/email/service.py` → шаблон `beta_invitation`.

Проверить в консольном режиме:
```bash
EMAIL_PROVIDER=console python3 -c "
from saas.email.service import EmailService
svc = EmailService()
svc.send_beta_invitation('test@test.com', 'Tester Name', 'ROMA-1090-F5B6')
"
```

## Запуск (день 1)

### 1. Разослать инвайты

Каждому бета-тестеру отправить письмо с инвайт-кодом через `send_beta_invitation()`.

### 2. Проверить onboarding flow

1. Тестер получает письмо с инвайт-кодом
2. Регистрируется на `/auth/signup` (обязательно с инвайтом)
3. Подтверждает email (`/auth/verify-email?token=...`)
4. Получает API-ключ
5. Выполняет первую задачу: `curl -X POST /submit ...`

### 3. Мониторинг beta-метрик

| Метрика | Как проверить |
|---------|--------------|
| Зарегистрировано пользователей | `GET /admin/invites` показывает current_users |
| Активировано (email verified) | `PG: SELECT count(*) FROM users WHERE email_verified=true` |
| Выполнено задач | `PG: SELECT count(*) FROM execution_jobs` |
| Spend-cap блокировки | Prometheus: `roma_spend_cap_blocked_total` |
| Баланс тенантов | `GET /billing/balance` с API-ключом администратора |
| Собрано фидбека | `GET /beta/leads` или `SELECT count(*) FROM feedback` |

## Во время беты (1–2 недели)

### Ежедневно

1. **Проверять health:** `curl /health`
2. **Мониторить ошибки:** логи `/dev/shm/roma-execution-bridge_err.log`
3. **Отвечать на фидбек:** в течение 24 часов
4. **Проверять spend-cap:** не блокирует ли активных пользователей

### Еженедельно

1. **Сводка по использованию:** количество активных пользователей, задач, GPU-часов
2. **Топ багов:** из фидбека
3. **Feature requests:** из фидбека
4. **Корректировка лимитов:** если spend-cap $5 слишком низкий — увеличить

### Действия при проблемах

| Проблема | Действие |
|----------|---------|
| Beta полна (423 Locked) | Увеличить BETA_MAX_USERS |
| Инвайт не работает | Деактивировать старый, создать новый |
| Spend-cap блокирует | Увеличить BETA_DEFAULT_SPEND_CAP_USD |
| Пользователь не верифицировал email | Отправить повторно `/auth/resend-verification` |

## Завершение беты

1. **Собрать весь фидбек:** экспортировать из БД
2. **Приоритизировать баги:** P0/P1/P2
3. **Принять решение:** открытая бета / публичный запуск / доработка
4. **Поблагодарить тестеров:** массовое письмо

## Чеклист готовности к бете

- [ ] BETA_MODE=true в .env
- [ ] BETA_MAX_USERS установлен
- [ ] BETA_REQUIRE_INVITE=true
- [ ] BETA_DEFAULT_SPEND_CAP_USD установлен
- [ ] Сгенерированы инвайт-коды
- [ ] Пригласительные письма протестированы
- [ ] Onboarding doc (ONBOARDING.md) готов
- [ ] Feedback endpoint работает
- [ ] Admin endpoint /admin/invites доступен
- [ ] /beta страница обновлена
- [ ] Сервис перезапущен с новыми env
- [ ] E2E тест: регистрация → верификация → submit → баланс

---

_Версия: 2026-08-20. ROMA Execution Bridge v2.1.0-beta._
