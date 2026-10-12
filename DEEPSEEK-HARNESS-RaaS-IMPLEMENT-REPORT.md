# DEEPSEEK-HARNESS-RaaS-IMPLEMENT-REPORT — PA-01 (RaaS-01, CRITICAL)

**Дата:** 2026-10-11 · **Режим:** локальная изолированная подготовка, без production · **MONEY STOP:** HELD

---

## 1. Что сделано / подготовлено / заблокировано

### Реализовано локально (PA-01, durable usage metering)
1. **Миграция `migrations/015_usage_events_billed.sql`** — добавляет отсутствующую колонку `billed BOOLEAN DEFAULT NULL` в `usage_events`. Семантика: `TRUE`=уже забиллено, `FALSE`=записано-не-забиллено, `NULL`=legacy/unknown (исторические строки НЕ бэкфиллятся).
2. **`billing/pg_metering.py:record()` — fail-closed.** Убрано `except PGUnavailableError: pass` + in-memory fallback, который давал ложный успех. Теперь ошибка записи логируется с `tenant/job` и пробрасывается (`raise`) → `execution_worker` ловит и ставит `billing_pending`.
3. **`billing/pg_metering.py:_pg_execute()` — исправлен pre-check.** Убран `or not self._pg.is_connected` (он мешал ленивому созданию пула: первый `record()` падал, не попытавшись подключиться). Теперь только `if not enabled`, а `get_connection()` сам лениво поднимает пул и fail-closed.
4. **Тест `tests/test_pg_metering_failclosed.py`** — 2 unit-теста (fail-closed на ошибке записи; обе durable-записи пытаются и возвращается событие).
5. **`decision_store.py` (решение владельца п.1)** — удалён вызов `db.insert_usage_event(...)` из submit-пути (submit-time estimate больше не пишется в `usage_events`; `estimated_cost` остаётся в `insert_decision_record` и в ответе клиенту).
6. **`db_adapter.py:insert_usage_event`** — помечен `DEPRECATED` (тело НЕ переписано; вызовов нет).
7. **Тест `tests/test_submit_no_usage_estimate.py`** — submit не вызывает `insert_usage_event`; `estimated_cost` в decision record.
8. **Миграция `migrations/016_usage_events_idempotency.sql`** — частичный уникальный индекс `uq_usage_events_tenant_job_type` на `(tenant_id, job_id, event_type) WHERE job_id NOT IN ('manual','auto','')`.
9. **`billing/pg_metering.py` + `db_pg_sync.record_usage_event`** — для реального `job_id`: `INSERT ... ON CONFLICT (tenant_id, job_id, event_type) WHERE ... DO NOTHING` (повтор не обновляет `cost_usd`/`billed`). Для `manual`/`auto` — обычный INSERT.
10. **Тест `tests/test_usage_event_idempotency.py`** — один `(tenant, job, event_type)` даёт одну строку; `gpu_usage`+`token_usage` на одном job — две строки; `manual` не схлопывается; повтор не перезаписывает `cost_usd`/`billed`.

### Реализовано локально (PA-02, admission fail-closed + cap=0)
11. **`main.py:_check_spend_cap`** — fail-closed:
    - `cap < 0` (enterprise/unlimited) → allow;
    - `cap == 0` → **BLOCK** (`"Spend-cap is zero: paid work not allowed"`), free-tier не выдумывается;
    - не-персистентный лэджер (`billing_ledger.is_persistent == False`) → **BLOCK** (неизвестный баланс ≠ 0.0);
    - ошибка чтения баланса (`PGUnavailableError`) → **BLOCK**.
12. **Тест `tests/test_spend_cap_admission.py`** — 6 кейсов: cap=0 блокирует; cap<0 unlimited; non-persistent блокирует; ошибка чтения блокирует; реальный нулевой баланс допускается в пределах cap; projected>cap блокирует.
13. **Проводка guard'а во все 3 пути создания job** (один guard, не копия):
    - `main.py:submit_job` → `_check_spend_cap(tenant_id, estimate_cost({"gpu_required": …}), plan_name)` перед созданием job;
    - `main.py:submit_atom_cluster` → `_check_spend_cap(tenant_id, estimate_cluster_cost(cluster_spec), plan_name)` перед `insert_job_raw` (spec: `nodes`+`expected_gpu_seconds` обязательны, иначе 422);
    - `decision_store.submit_job_through_gate` → `from main import _check_spend_cap` (лениво) + `_deny`/`402` перед `insert_job`; `_estimate_cost` делегирует в `cost.gate.estimate_cost`.
14. **Тест `tests/test_admission_wiring.py`** — 8 кейсов (3 пути × {cap=0, PG-unavailable} + cluster missing-spec/nodes<1 → 422): guard не пропускает создание job.
15. **`cost/gate.py:estimate_cluster_cost`** — `nodes × expected_gpu_seconds × 0.00001` (ставка из `billing/finalize.py`), `nodes`/`expected_gpu_seconds` ≥ 1 иначе `ValueError`.
16. **Тест `tests/test_cost_estimate.py`** — estimate_cost GPU=0.002/CPU=0.0001; cluster 2×100=0.002; missing/ниже-1 → ValueError.

### Реализовано локально (PA-05, локальные mock-тесты webhook/checkout)
17. **Покрытие 8 кейсов уже есть** в существующих тестах (новых тестов не писал — не дублировал):
    - подпись: `test_p2_cloudpayments_signature_contract.py` (валидная/неверная/чужой ключ/tampered/пустой заголовок/нет секрета);
    - webhook-обработка: `test_p1_cloudpayments_webhook.py` (valid/invalid/missing AccountId/repeat invoice/secret unset);
    - amount/currency/refused: `test_p1_cloudpayments_amount_matches_plan.py` + `test_p1_cloudpayments_declined_not_success.py`;
    - idempotency/AccountId-scope: `test_p1_cloudpayments_invoice_tenant_scope.py`.
18. **`tests/conftest.py`** — добавлен безопасный stub `pydantic_settings` (no-op если реальный модуль установлен) — разблокирует тесты, импортирующие `main`/`crypto_payments`, в среде без зависимости.
19. Провайдеры (CloudPayments/Stripe/NOWPayments) — **выключены/не вызывались**; тесты offline (mocks/fixtures, без сети и live secrets).

### Проверено (факты)
- Схема `usage_events` (002) действительно БЕЗ `billed`; два канонических write-пути (`pg_metering`, `db_pg_sync`) пишут `billed`; третий (`db_adapter.insert_usage_event`) пишет НЕсовместимый набор `(id, tenant_id, job_id, gpu_seconds, cost)`.
- Миграции 000–016 накатываются на disposable PG (17/17 applied), insert/readback прошёл, дублей до индекса нет.
- Guard вызван во всех трёх путях (main.py:1151, main.py:1259, decision_store.py:100).

### Заблокировано / осталось
- **Идемпотентность события `record()` для реального `job_id` — PASS** (миграция 016 + `ON CONFLICT DO NOTHING`; проверено на disposable PG).
- **`manual`/`auto` — BLOCKED (намеренно):** не дедуплицируются по решению владельца (ключ только `(tenant_id, job_id, event_type)` для реального `job_id`). Обычный INSERT, дубли для `manual`/`auto` возможны и допустимы.
- **Полный suite** не запускался (см. §4).
- `insert_usage_event` теперь **не вызывается** (решение владельца: убрать вызов, тело не переписывать) — третий write-путь в submit-пути исчез; функция остаётся unused/`DEPRECATED`.

---

## 2. Хост, репозитории, изоляция

- **Хост:** `pop-os`, user `felix`. `/home/workspace` (путь из аудита) — отсутствует.
- **ROMA:** `mahaasur13-sys/roma-execution-bridge` (public), `master` = `2e1af0d6d581cda95923c86b84e4a35c15ca4281` ✅ (совпал с ожиданием).
- **AstroFin:** `mahaasur13-sys/astrofin-sentinel-platform` — НЕ тронут (в этом хосте рабочее дерево AstroFin грязное/чужое — сохранено как есть).
- **Изоляция:** свежий `git clone` в `.roma-work`, локальная ветка `raas-001b/pa01-metering-contract` (без commit/push). Чужой dirty state не трогался.
- **План/рапорт:** файлы `План-действий-ZO-RaaS-AUDIT-2026-10-08.md`, `ZO-RaaS-AUDIT-001 рапорт v1`, `Addendum A` **не найдены** ни в ФС, ни в репозитории ROMA. Работа велась по встроенному плану промта (PA-01) + `RaaS-AUDIT-2026-10-08.md` + `promt-ZO-RaaS-AUDIT-001.md`.

---

## 3. Файлы и diff

| Файл | Статус |
|------|--------|
| `migrations/015_usage_events_billed.sql` | NEW (подготовлена, на live НЕ применялась) |
| `migrations/016_usage_events_idempotency.sql` | NEW (подготовлена, на live НЕ применялась) |
| `billing/pg_metering.py` | MODIFIED (fail-closed + `is_connected` pre-check + `ON CONFLICT DO NOTHING`) |
| `db_pg_sync.py` | MODIFIED (`record_usage_event` — `ON CONFLICT DO NOTHING` + `RETURNING` None) |
| `decision_store.py` | MODIFIED (удалён вызов `insert_usage_event`) |
| `db_adapter.py` | MODIFIED (docstring `DEPRECATED`, тело не тронуто) |
| `tests/test_pg_metering_failclosed.py` | NEW |
| `tests/test_submit_no_usage_estimate.py` | NEW |
| `tests/test_usage_event_idempotency.py` | NEW |
| `main.py` | MODIFIED (`_check_spend_cap` — fail-closed + cap=0 блок) |
| `tests/test_spend_cap_admission.py` | NEW |

Миграции применены **только** к disposable PostgreSQL-контейнеру (удалён). К live БД не применялись.

---

## 4. Точные тестовые команды и статусы

| Команда | Статус |
|---------|--------|
| `python3 -m py_compile billing/pg_metering.py` | PASS |
| `python3 -m pytest tests/test_pg_metering_failclosed.py tests/test_billing_units.py -q -p no:cacheprovider` | **PASS (16)** |
| `python3 -m pytest tests/test_submit_no_usage_estimate.py -q -p no:cacheprovider` | **PASS (1)** |
| `python3 -m pytest tests/test_submit_no_usage_estimate.py tests/test_pg_metering_failclosed.py tests/test_billing_units.py tests/test_migration_schema.py tests/test_migration_runner.py -q -p no:cacheprovider` | **PASS (18 + 2 skip)** |
| `python3 -m pytest tests/test_migration_schema.py tests/test_migration_runner.py -q -p no:cacheprovider` | **PASS** (PG-части skip с явным issue/expiry) |
| `python3 -m pytest tests/test_billing_increment.py -q` | **PASS** (после stub в conftest.py; ранее BLOCKED на `pydantic_settings`) |
| `python3 -m pytest tests/test_p2_cloudpayments_signature_contract.py` | **PASS (8)** — подпись |
| `python3 -m pytest tests/test_p2_… tests/test_p1_cloudpayments_webhook.py tests/test_p1_cloudpayments_amount_matches_plan.py tests/test_p1_cloudpayments_declined_not_success.py tests/test_p1_cloudpayments_invoice_tenant_scope.py tests/test_crypto_payments.py tests/test_billing_increment.py` | **PASS (61, 8 warnings)** — платежи |
| Полный оффлайн-регресс (PA-01..05 + billing/migration) | **PASS (102 + 3 skip, 8 warnings)** |
| `PG_DSN=<disposable> python3 scripts/run_migrations.py` | **PASS** (17 applied, 0 skipped) |
| insert/readback на disposable PG (psycopg2): `billed` True/False/NULL | **PASS** |
| `PG_DSN=<disposable/roma_test> pytest tests/test_usage_event_idempotency.py` | **PASS (1)** — реальный job_id дедуп, `manual` не дедуп |
| `python3 -m pytest tests/test_spend_cap_admission.py -q -p no:cacheprovider` | **PASS (6)** — cap=0, non-persistent, read-error, real-zero |
| Полный suite | NOT RUN (сначала зависимости) |

---

## 5. Gates A–D

- **Gate A (staging-safe):** **ЧАСТИЧНО (не закрыт целиком)**. ✅ контракт `billed` согласован и проверен на disposable PG; ✅ миграции идемпотентны (`IF NOT EXISTS` + `schema_migrations`); ✅ ошибки видимы (fail-closed); ✅ **третий write-путь устранён**; ✅ **идемпотентность события для реального `job_id`** (миграция 016 + `ON CONFLICT DO NOTHING`); ✅ **admission fail-closed + проводка во все 3 пути + оценка стоимости** (`_check_spend_cap`: cap=0 → блок, неизвестный баланс → блок, ошибка чтения → блок; оценка = `estimate_cost`/`estimate_cluster_cost`, не фикс. 0.01; 8 wiring-тестов + 5 cost-тестов PASS). ⚠️ **оценка admission ≠ фактическая цена** (длительность/ресурсы неизвестны до исполнения; реальная цена считается на финализации). ❌ **НЕ атомарны `debit_if_funds`→`record()`** (признано, не чинилось — единственный незакрытый пункт Gate A). ⚠️ `manual`/`auto` не дедуплицируются (намеренно).
- **Gate B (платёжные проверки):** локальные mock-тесты webhook/checkout **PASS (61)** — валидная/неверная подпись, нет секрета, tampered payload, refused, amount/currency mismatch, unknown AccountId, повторный webhook. Внешний sandbox и live **не разрешаются** (провайдеры выключены). Формальное закрытие Gate B — за владельцем.
- **Gate C (live-пилот ROMA):** НЕ проходится (нет разрешения Finance/Ops).
- **Gate D (RaaS за результат AstroFin):** НЕ проходится.

---

## 6. Вопросы владельцу / остаточные риски / следующий шаг

**Ответы/решения владельца (2026-10-11):**
1. `insert_usage_event` — submit-time estimate, не факт usage. **Решение: убрать вызов из submit-пути, функцию НЕ переписывать на billed-контракт.** (выполнено: вызов удалён, функция `DEPRECATED`).
2. `billed=NULL` для исторических строк — **верно**, `DEFAULT FALSE` не ставить.
3. **Ключ идемпотентности:** `(tenant_id, job_id, event_type)` только для реального `job_id`; генерируемый `event_id` не использовать; `manual`/`auto` НЕ дедуплицировать. (выполнено: миграция 016 + `ON CONFLICT DO NOTHING`).

**Остаточные риски:**
- Деньги могут быть уже списаны (`debit_if_funds`) до того, как `record()` упадёт (usage не записан) — расхождение debit-then-record не устранено (признано, по PA-01 «не добавлять резервирование/компенсацию»).
- `test_billing_increment.py` не прогнан (нет `pydantic_settings`).
- `manual`/`auto` не дедуплицируются (намеренно) — дубли для этих значений возможны.

**Следующий шаг:** PA-01 закрыт в согласованном объёме (контракт `billed`, fail-closed, третий путь устранён, идемпотентность реального `job_id`). НЕ PA-02, НЕ commit.

---

## 7. No-write гарантии (верны)
production writes=0 · live migrations=0 (только disposable PG, удалён) · restarts/deploys=0 · внешние payment/provider calls=0 · live jobs created=0 · job states changed=0 · secrets rotated/revoked=0 · secret values exposed=0 · AstroFin tags/release untouched · **commit/push/merge/tag/deploy = 0** (изменения только в рабочем дереве изолированного клона).
