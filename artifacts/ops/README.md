# artifacts/ops — выгрузка операционного кода (N6)

Операционный код, который что-то делает в проде, обязан быть восстановим из репозитория.
Канонические пути — `deploy/ops/*`; здесь лежат снимки ФАКТИЧЕСКИ исполнявшихся файлов
(tree B) на дату снимка, чтобы расхождение можно было доказать, не имея доступа к tree B.

## Снимки 2026-09-21

| файл | sha256 (первые 16) | исполняемый путь (tree B) |
|------|--------------------|---------------------------|
| `pg_watchdog.executed-2026-09-21.py` | `59f7b4a45b726843` | `/home/workspace/artifacts/pg-watchdog/pg_watchdog.py` |
| `test_budget_exceeded.script-2026-09-21.py` | см. `sha256sum` | `/home/workspace/artifacts/pg-watchdog/test_budget_exceeded.py` |

Канон в репозитории: `deploy/ops/pg_watchdog.py` — на 2026-09-21 совпадает со снимком
(`59f7b4a4…`), то есть содержит фикс R3 (state/журнал на `/var/lib/pg-watchdog/`).
Ранее tracked-версия отставала ровно на R3 (21 строка) — это и был дефект N6.

R5-доказательство канонизировано как pytest-тест: `tests/test_pg_watchdog_budget.py`
(маркер `ops`), проверяет именно канонический файл `deploy/ops/pg_watchdog.py`.

## Drift-контроль

Один скрипт с манифестом пар «канон → исполняемое»: `scripts/drift_check.py`,
манифест `deploy/ops/executed_paths.json`.

```bash
python3 scripts/drift_check.py            # на ноде: сравнение sha256, алерт через alert-relay, exit≠0
python3 scripts/drift_check.py --ci       # на раннере: отсутствие executed_path — warning
```

Политика: расхождение — НИКОГДА не автоперезапись. Направление канона repo → executed,
применение — вручную с GO: авто-reconcile отставшим репозиторием молча откатил бы живой фикс.

Восстановление исполняемой копии из канона (ручной шаг, по GO):

```bash
sha256sum deploy/ops/pg_watchdog.py /home/workspace/artifacts/pg-watchdog/pg_watchdog.py
cp deploy/ops/pg_watchdog.py /home/workspace/artifacts/pg-watchdog/pg_watchdog.py
```
