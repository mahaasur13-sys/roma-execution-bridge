# Beta Invitation Email Template

ROMA Execution Bridge — приглашение в закрытое бета-тестирование.

## HTML-шаблон

Адаптивный, тёмная тема (GitHub-стиль), с кнопкой CTA и UTM-параметрами.

Переменные:
- `{{ name }}` — имя получателя (fallback: "Valued Tester")
- `{{ email }}` — email получателя
- `{{ invitation_link }}` — полная ссылка на дашборд с UTM-параметрами

## Plain Text

Текстовая альтернатива для почтовых клиентов без поддержки HTML.

## Персонализация

```python
name = lead.get("company", "") or lead.get("role", "") or "Valued Tester"
invitation_link = f"{DASHBOARD_URL}&email={email}"
```

## UTM-параметры

```
?ref=beta&utm_source=email&utm_medium=invite&email={{ email }}
```

## Демо-ключ

`roma-demo-key-2026` — единый для всех бета-тестеров.

## Отправка

```bash
# Dry-run (без реальной отправки)
python scripts/send_invitations.py --dry-run --limit 10

# Реальная отправка (требуется SENDGRID_API_KEY)
python scripts/send_invitations.py --limit 10

# Через админ-эндпоинт
curl -X POST https://roma-execution-bridge-asurdev.zocomputer.io/admin/invite \
  -H "X-API-Key: roma-demo-key-2026" \
  -H "Content-Type: application/json" \
  -d '{"limit": 10, "dry_run": true}'
```

## Шаблон письма

См. `scripts/send_invitations.py` — константы `HTML_TEMPLATE` и `PLAIN_TEXT_TEMPLATE`.
