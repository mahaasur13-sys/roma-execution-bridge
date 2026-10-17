# Jev Shadow — Операционный ранбук (включение / проверка / rollback)

Дата: 2026-10-16. Область: **R1/R3/R5**. R2 — не трогаем. Пороги — заглушки (не менять).
Cascade-флаги — **не включать**.

## 0. Ключевой факт о проводке (читать первым)

Только **R5 реально подключена** к продакшен-шву
(`plugins/builtin/support_chat.py::_detect_intent` зовёт `shadow_triage`).

**R1** (`cost/jev_shadow.py::shadow_evaluate`) и **R3**
(`plugins/builtin/jev_policy.py::shadow_run`) — построены и покрыты тестами, но
**ещё не вызываются** на местах `gate.evaluate()` / `policy_engine.run()`.

Следствие: пока R1/R3 не подключить (отдельный PR, 1–2 строки на вызов),
флаги `JEV_GATE_SHADOW` / `JEV_POLICY_SHADOW` сами по себе данных **не дадут**.

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
| `JEV_TRIAGE_SHADOW=1` | R5 | ✅ подключена |
| `JEV_GATE_SHADOW=1` | R1 | ⚠️ нужна проводка (§0) |
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

## 4. Rollback

Выключение = снять флаги (по умолчанию они и так off):
- `unset JEV_GATE_SHADOW JEV_POLICY_SHADOW JEV_TRIAGE_SHADOW`
- Для полной остановки сети: `JEV_ENABLED=0` (клиент становится no-op → `JevDisabled`).
- Файл `JEV_SHADOW_LOG` можно оставить (накопленные данные) или перенести/архивировать.
- Cascade не включался → отдельного rollback каскада нет.
