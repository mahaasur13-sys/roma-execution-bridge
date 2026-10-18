# Jev Shadow — Операционный ранбук (включение / проверка / rollback)

Дата: 2026-10-16. Область: **R1/R3/R5**. R2 — не трогаем. Пороги — заглушки (не менять).
Cascade-флаги — **не включать**.

## 0. Ключевой факт о проводке (читать первым)

Проверено 2026-10-18 на `master` f48276a:

- **R1 подключён**: `cost/jev_shadow.py::shadow_evaluate` вызывается из
  `routers/submit.py:110` (публичный `POST /v1/jobs`). `JEV_GATE_SHADOW=1` даёт данные.
- **R5 привязан к несмонтированному плагину**: `shadow_triage` зовётся только из
  `plugins/builtin/support_chat.py::_detect_intent`, а плагин `support-chat` в приложении
  не смонтирован (в живом `/openapi.json` нет `/v1/plugins*`, `PluginManager` вне `plugins/`
  не вызывается). Реальный чат `POST /v1/support/*` (`support_chat/router.py`) идёт мимо
  плагина и категорию сообщения не определяет. **В проде R5 неактивен: `JEV_TRIAGE_SHADOW=1`
  сам по себе данных не даст.**
- **R3 не подключён**: `plugins/builtin/jev_policy.py::shadow_run` покрыт тестами, но вызов
  остаётся unwired (`cascade_run` — escalate-only, не подключён).

Следствие: сейчас в shadow-лог пишет только R1.

## 1. Переменные окружения

Клиент (`core/jev_client.py`) выполняет вызов только при `JEV_ENABLED` + `OPENROUTER_API_KEY`.

| Переменная | Значение | Назначение |
|------------|----------|-----------|
| `OPENROUTER_API_KEY` | `sk-or-...` | аутентификация клиента |
| `JEV_ENABLED` | `1` | гейт клиента (иначе `JevDisabled`) |
| `JEV_SHADOW_LOG` | `/abs/jev_shadow.jsonl` | путь JSONL (default `logs/jev_shadow.jsonl`) |

По точкам (только shadow):

| Переменная | Точка | Проводка |
|------------|-------|----------|
| `JEV_TRIAGE_SHADOW=1` | R5 | ⚠️ в проде неактивна: плагин не смонтирован (§0) |
| `JEV_GATE_SHADOW=1` | R1 | ✅ подключена (`routers/submit.py:110`) |
| `JEV_POLICY_SHADOW=1` | R3 | ⚠️ нужна проводка (§0) |

**НЕ ставить:** `JEV_POLICY_CASCADE`, `JEV_TRIAGE_CASCADE` (cascade запрещён на этом этапе),
`JEV_*_SHADOW_SEND_UNTRUSTED` (не шлём сырые тексты).

## 2. Проверка аутентификации и записи

```bash
# smoke: клиент аутентифицируется и возвращает ответ (не бросает JevError)
OPENROUTER_API_KEY=sk-or-... JEV_ENABLED=1 python -c "
from core.jev_client import JevClient, JevError
try:
    r = JevClient(timeout=5).evaluate({'message_length': 5}, {'triage': {'type':'choice','criteria':{'a':'x','b':'y'}}})
    print('OK', sorted(r.get('answers', {})))
except JevError as e:
    print('FAIL', e)
"
```

Затем прогнать трафик (или вызвать R5-плагин) и убедиться:
1. `JEV_SHADOW_LOG` растёт (`tail -f $JEV_SHADOW_LOG`).
2. В логах нет массовых `jev ... shadow failed` (единичные — ок: fail-closed = деградация,
   вердикт не меняется, просто нет записи).

## 3. Проверка, что детерминированные вердикты не изменились

По построению shadow — read-only (вердикт = детерминированный, Jev только наблюдает;
это покрыто 167 тестами). Практическая проверка:
1. Прогнать один и тот же набор запросов дважды — флаги OFF и флаги ON.
2. Сравнить детерминированные исходы (R5: строки intent; R1/R3: verdict) — должны совпасть 1:1.
3. Ошибка/таймаут Jev в shadow не меняет вердикт (fail-closed → просто нет записи в логе).

## 4. Метрики

`monitoring/metrics.py` (дефолтный registry, отдаётся на `GET /metrics`):

| Метрика | Лейблы | Смысл |
|---------|--------|-------|
| `roma_jev_shadow_observations_total` | `point`, `outcome` (`ok`/`fallback`) | наблюдение записано / ушло в fallback |
| `roma_jev_shadow_fallback_total` | `point`, `reason` (`jev_error`/`timeout`/`rate_limited`/`no_answer`/`state_capture`) | почему вердикт остался детерминированным |
| `roma_jev_shadow_agreement_total` | `point`, `agree` (`yes`/`no`) | класс Jev совпал с детерминированным вердиктом (R1: `normal` ↔ `allowed`) |
| `roma_jev_shadow_dropped_total` | `point` | наблюдение выброшено bounded-очередью `core/jev_observer.py` (переполнение или сбой submit) |
| `roma_jev_shadow_latency_seconds` | `point` | латентность одного вызова Jev (buckets 0.05…8 с) |

Пишет `track_jev_shadow(point, outcome=…, reason=…, agree=…, dropped=…, latency_s=…)`.
Лейблы — только из фиксированных множеств, неизвестное значение схлопывается в `other`:
ни `tenant_id`, ни текста сообщения в метриках нет. Сбой телеметрии вердикт не меняет.

Что считать: доля fallback = `fallback_total / observations_total`; доля несовпадений =
`agreement_total{agree="no"} / (agree="yes" + agree="no")`; дропы = `dropped_total`
(рост = Jev медленнее бюджета очереди, наблюдения теряются).

JSONL (`JEV_SHADOW_LOG`) пишется и на fallback: `schema=1`, `outcome`, `reason`, `latency_ms`
плюс прежние поля (`choice`, `prob_anomaly`, `zone`, `verdict`, `observed_at`).

## 5. Rollback

Выключение = снять флаги (по умолчанию они и так off):
- `unset JEV_GATE_SHADOW JEV_POLICY_SHADOW JEV_TRIAGE_SHADOW`
- Для полной остановки сети: `JEV_ENABLED=0` (клиент становится no-op → `JevDisabled`).
- Файл `JEV_SHADOW_LOG` можно оставить (накопленные данные) или перенести/архивировать.
- Cascade не включался → отдельного rollback каскада нет.
