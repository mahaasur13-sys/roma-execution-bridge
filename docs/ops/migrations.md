# Миграции БД ROMA

## Ролевая модель (факт на 2026-10-28)

| Роль | Атрибуты | Назначение |
| --- | --- | --- |
| `postgres` | SUPERUSER, LOGIN | только админ: `pg_dump`, ротация паролей, ручные разборы. Рантайм и миграции под ним не ходят |
| `roma_owner` | NOLOGIN | владелец всех объектов в `public` (таблицы, sequences, views, функции). Под ним исполняется тело каждой миграции |
| `roma_app` | LOGIN, NOSUPERUSER | рантайм сервиса: `SELECT`/`INSERT`/`UPDATE` + sequences. `CREATE` на схему не выдан → DDL из приложения невозможен |
| `ro_usage` | LOGIN | внешние отчёты по usage (2 таблицы) |

Инвариант: **в `public` нет объектов, владелец которых не `roma_owner`.** Раннер проверяет это после
применения и возвращает `rc=1` со списком имён, если инвариант нарушен.

## `ROMA_DB_INIT_DDL=0`

Рантайм больше не создаёт таблицы на старте. Новая таблица/индекс/sequence существует только после
миграции в `migrations/`. Если таблицы нет, сервис падает на старте fail-closed и перечисляет
отсутствующие имена — это ожидаемое поведение, а не дефект.

## Раннер

`scripts/apply_migrations.py` — роль-aware раннер для окна миграций:

- админский DSN: `ROMA_ADMIN_PG_DSN` в окружении либо файл из `--dsn-file` (голая строка DSN или
  `ROMA_ADMIN_PG_DSN=...`). DSN не печатается;
- файлы `migrations/*.sql` в лексикографическом порядке, уже записанные в `schema_migrations`
  пропускаются;
- каждая миграция — своя транзакция: `SET LOCAL ROLE roma_owner` → тело файла →
  `INSERT INTO schema_migrations`. Явные `BEGIN;`/`COMMIT;` из тела вырезаются, транзакцией владеет раннер;
- `--dry-run` только печатает pending, ничего не пишет;
- нет роли `roma_owner` → `rc=2` (fail-closed), ни одного изменения.

`scripts/run_migrations.py` остаётся тем, что вызывает deploy-агент (`deploy/ops/zo_deploy_agent.py`):
он открывает соединение под сервисным DSN и не переключает роль. `apply_migrations.py` — ручной
ops-инструмент для окна с админским DSN; автоматический деплой на него пока не переведён.

## Порядок применения

```bash
# 1. pending (read-only)
python3 scripts/apply_migrations.py --dry-run --dsn-file /root/.sealed/roma-pg-dsn.txt

# 2. бэкап ДО
pg_dump -Fc -d "$ADMIN_DSN" -f /root/.sealed/roma-pre-$(date -u +%Y%m%dT%H%M%SZ).dump
chmod 400 /root/.sealed/roma-pre-*.dump

# 3. применение
python3 scripts/apply_migrations.py --dsn-file /root/.sealed/roma-pg-dsn.txt

# 4. рестарт сервиса (миграции — раньше рестарта)
supervisorctl -c /etc/zo/supervisord-user.conf restart roma-execution-bridge

# 5. пост-чек
curl -s 127.0.0.1:8900/health          # pg=true
curl -s 127.0.0.1:8900/openapi.json | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["paths"]))'
psql "$ADMIN_DSN" -X -A -t -c "select filename from schema_migrations order by applied_at desc limit 3"
```

Пост-чек сверяется со снимком до окна: PID сменился, `/health` `pg=true`, число paths не уменьшилось,
`execution_jobs` по статусам и `ledger_entries` (count/sum) без изменений, в логах 0 `Traceback`,
firing — только `Watchdog`, в `/dev/shm/*.log` нет `permission denied`.

## Откат

Down-миграций нет: схема откатывается восстановлением дампа.

```bash
supervisorctl -c /etc/zo/supervisord-user.conf stop roma-execution-bridge
pg_restore -d "$ADMIN_DSN" --clean --if-exists /root/.sealed/roma-pre-<ts>.dump
supervisorctl -c /etc/zo/supervisord-user.conf start roma-execution-bridge
```

Если миграция оставила новый объект, а данные не тронуты, достаточно `DROP` этого объекта под
`roma_owner` и удаления строки из `schema_migrations` — но только когда дамп не нужен.

## Что проверять, если раннер отказал

- `rc=1` со списком объектов → в `public` есть объект чужого владельца: `ALTER ... OWNER TO roma_owner`
  отдельной миграцией, а не вручную;
- `rc=1` с `migration failed` → тело упало, транзакция откатана, `schema_migrations` не тронута;
- `rc=2` → нет DSN или нет роли; проверить `ROMA_ADMIN_PG_DSN` / `--dsn-file` и `pg_roles`.
