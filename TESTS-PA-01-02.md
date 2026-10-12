# TESTS — PA-01 & PA-02 (23 новых теста)

Описание 23 тестов, добавленных в `raas-001b/pa01-metering-contract` (commit 67bdcdb).
Все — локальные (mocks/fixtures); сеть, live-секреты, платёжные провайдеры и live-БД не задействуются.

## PA-01 — durable usage metering (RaaS-01)

| Файл | Тест | Что проверяет |
|------|------|----------------|
| `test_pg_metering_failclosed.py` | `test_record_fails_closed_when_pg_write_fails` | `PGMeteringEngine.record()` при ошибке записи **пробрасывает** `PGUnavailableError` (не молчаливый in-memory fallback) |
| `test_pg_metering_failclosed.py` | `test_record_attempts_both_durable_writes_and_returns_event` | `record()` делает обе durable-записи (`usage_insert` + `usage_totals_upsert`) и возвращает событие |
| `test_usage_event_idempotency.py` (PG) | `test_usage_event_idempotency` | миграция 016 + `ON CONFLICT DO NOTHING`: один `(tenant, job_id, event_type)` дважды → 1 строка; `gpu+token` на одном job → 2 строки; `manual` не схлопывается; повтор не перезаписывает `cost_usd`/`billed` |
| `test_submit_no_usage_estimate.py` | `test_submit_does_not_write_estimate_into_usage_events` | submit-путь **не** пишет оценку стоимости в `usage_events`; `estimated_cost` остаётся в decision record |

## PA-02 — admission fail-closed + cap=0 (RaaS-04)

| Файл | Тест | Что проверяет |
|------|------|----------------|
| `test_spend_cap_admission.py` | `test_cap_zero_blocks_paid_work` | `cap=0` → BLOCK (платная работа не пропускается) |
| `test_spend_cap_admission.py` | `test_negative_cap_is_unlimited` | `cap<0` (enterprise) → allow |
| `test_spend_cap_admission.py` | `test_non_persistent_ledger_blocks` | не-персистентный лэджер → BLOCK (неизвестный баланс ≠ 0.0) |
| `test_spend_cap_admission.py` | `test_balance_read_error_blocks` | ошибка чтения баланса → BLOCK |
| `test_spend_cap_admission.py` | `test_real_zero_balance_admits_within_cap` | реальный нулевой баланс (durable) в пределах cap → allow |
| `test_spend_cap_admission.py` | `test_projected_over_cap_blocks` | `projected > cap` → BLOCK |
| `test_admission_wiring.py` | `test_cluster_cap_zero_does_not_create_job` | кластер: cap=0 → 402, job не создан |
| `test_admission_wiring.py` | `test_cluster_pg_unavailable_does_not_create_job` | кластер: PG-unavailable → 402, job не создан |
| `test_admission_wiring.py` | `test_cluster_missing_spec_does_not_create_job` | кластер без `nodes`/`expected_gpu_seconds` → 422, job не создан |
| `test_admission_wiring.py` | `test_cluster_nodes_below_one_does_not_create_job` | `nodes<1` → 422, job не создан |
| `test_admission_wiring.py` | `test_submit_job_cap_zero_does_not_create_job` | `submit_job`: cap=0 → 402, job не создан |
| `test_admission_wiring.py` | `test_submit_job_pg_unavailable_does_not_create_job` | `submit_job`: PG-unavailable → 402, job не создан |
| `test_admission_wiring.py` | `test_gate_cap_zero_does_not_create_job` | `submit_job_through_gate`: cap=0 → 402, job не создан |
| `test_admission_wiring.py` | `test_gate_pg_unavailable_does_not_create_job` | `submit_job_through_gate`: PG-unavailable → 402, job не создан |

## PA-02 — оценка стоимости на admission

| Файл | Тест | Что проверяет |
|------|------|----------------|
| `test_cost_estimate.py` | `test_estimate_cost_gpu` | GPU job → 0.002 |
| `test_cost_estimate.py` | `test_estimate_cost_cpu` | CPU job → 0.0001 |
| `test_cost_estimate.py` | `test_estimate_cluster_cost_nodes_times_seconds` | кластер 2×100 → 0.002 (`nodes × seconds × 0.00001`) |
| `test_cost_estimate.py` | `test_estimate_cluster_cost_missing_fields_raise` | нет `nodes`/`expected_gpu_seconds` → `ValueError` |
| `test_cost_estimate.py` | `test_estimate_cluster_cost_below_one_raises` | `nodes<1`/`expected_gpu_seconds<1` → `ValueError` |

## Итог
- Всего: **23 теста** (22 non-PG + 1 PG `test_usage_event_idempotency`).
- Без сети, без live-секретов, без провайдеров, без live-БД.
