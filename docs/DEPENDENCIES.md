# Зависимости ROMA: pyproject → uv.lock → requirements-*.txt

P-DEPS-LOCK (2026-11-01). Один канон вместо трёх расходящихся списков.

| Файл | Роль | Кто правит |
| --- | --- | --- |
| `pyproject.toml` | **прямые** зависимости и диапазоны: `[project].dependencies` (рантайм), extra `tracing` (OTLP), группы `dev` (pytest) и `ci` (dashboard/scripts), `[tool.uv].constraint-dependencies` (полы безопасности транзитивов) | человек |
| `uv.lock` | **канон**: полный резолв с хешами | `uv lock` |
| `requirements-ci.txt` | **производный**: экспорт lock для CI (`pip install -r`) | `bash scripts/gen_requirements.sh` |
| `requirements-runtime.txt` | **производный**: рантайм без dev/ci, с хешами — для изолированного venv сервиса | `bash scripts/gen_requirements.sh` |

`requirements-worker.txt` (GPU-воркер, отдельный образ) в эту цепочку пока не входит.

## Как менять зависимость

1. Правка в `pyproject.toml` (новый пакет / диапазон / пол безопасности).
2. `uv lock` — для точечного подъёма: `uv lock --upgrade-package <имя>`; массовый `uv lock --upgrade` не делаем.
3. `bash scripts/gen_requirements.sh`.
4. Коммит всех трёх (четырёх) файлов вместе.

CI-джоба `deps-drift` (`.github/workflows/ci.yml`, uv 0.13.6) валится, если `uv lock --check`
не проходит или закоммиченные `requirements-*.txt` отличаются от свежего экспорта.
`security.yml` аудирует оба производных файла (`pip-audit -r`).

Замечания:
- Cooldown: `[tool.uv] exclude-newer = "7 days"` — резолв не берёт версии моложе 7 дней
  (semgrep `uv-missing-dependency-cooldown`). Исключение `exclude-newer-package` — только для
  security-фикса без более старой исправленной версии (сейчас pyasn1 0.6.5). Если свежий фикс
  нужен срочно, исключение добавляется точечно и с комментарием, а не снятием cooldown.
- `prometheus-client` = 0.8.0 не «пин из прошлого», а следствие `prometheus-fastapi-instrumentator<2.0`
  (последний 1.x = 1.3.0 требует `prometheus-client>=0.8.0,<0.9.0`). Подъём — отдельная задача
  (instrumentator 7.x, проверка `/metrics`).
- `fastapi>=0.136,<0.137`: на 0.141.x `include_router()` не монтирует маршруты (6 route-surface
  проверок краснеют) — история в G-CI-DEPS-INCOMPLETE.
- `oauthlib` 4.x приходит транзитивно (kubernetes → requests-oauthlib); ROMA его напрямую не
  использует. Ломающие изменения 4.0 касаются серверной (provider) части и JSONP в revocation;
  клиентский путь `OAuth2Session` (OIDC-refresh kube_config) не затронут.

## Изолированный рантайм-venv

```bash
# вариант A — из lock (нужен uv и checkout репо)
UV_PROJECT_ENVIRONMENT=/opt/roma-venv uv sync --frozen --no-dev --extra tracing --no-install-project
# вариант B — без uv, только pip
python3.12 -m venv /opt/roma-venv
/opt/roma-venv/bin/pip install --require-hashes -r requirements-runtime.txt
```

Сервис запускается из корня checkout (`main.py`), поэтому сам проект в venv не ставится.
`--extra tracing` оставляет OTLP-трассировку доступной (сегодня на Zo OTel установлен);
без него `main._setup_tracing()` честно выключает трассировку по ImportError.

## PROPOSAL — миграция Zo на изолированный venv (НЕ ВЫПОЛНЕНО, нужен отдельный GO)

Факт на 2026-11-01: `roma-execution-bridge` на Zo работает в общем системном
`/usr/local/bin/python3` (3.12.1, 445 пакетов: dev-инструменты, ML, editable-копии AstroFin),
версии стоят по старому `requirements-ci.txt`, а не по lock. Слияние этого PR на Zo пакеты
**не меняет**: `deploy/ops/zo_deploy_agent.py` делает fetch → ff → compileall → миграции →
рестарт и зависимостей не ставит.

Предлагаемая процедура (каждый шаг — по GO владельца, с отчётом):

1. **Подготовка (без влияния на прод).** `python3.12 -m venv /opt/roma-venv` →
   `pip install --require-hashes -r requirements-runtime.txt` с SHA master, который сейчас
   выкачен. `pip check`, `pip-audit -r requirements-runtime.txt --no-deps --disable-pip`.
2. **Smoke вне сервиса.** Из checkout: `/opt/roma-venv/bin/python -c "import main"`
   (с тем же окружением, что у сервиса), затем временный экземпляр на свободном порту
   (не 8900) → `/health`, `/billing/*` и админ-маршруты в `app.routes` (route-surface).
   Отличия от текущего прода в рантайм-наборе: 40 из 79 пакетов (среди заметных kubernetes
   29→31, psutil 5→6, websockets 15→17, wrapt 1→2, structlog 25→26, protobuf 6→7).
3. **Переключение.** Резервная копия `/root/.sealed/roma-start.sh`; в нём заменить
   интерпретатор на `/opt/roma-venv/bin/python` (только эта строка; секреты в скрипте не
   трогаются и не выводятся). `supervisorctl restart roma-execution-bridge` → `/health`,
   `readlink -f /proc/<pid>/exe` указывает на базу venv, `sys.prefix` = `/opt/roma-venv`.
4. **Наблюдение.** Логи сервиса и алерты 30–60 мин; деплой-агент продолжает работать как раньше.
5. **Откат.** Вернуть резервную копию `roma-start.sh` → `supervisorctl restart` → `/health`.
   Системный Python не трогается на всех шагах, поэтому откат мгновенный.
6. **Дальше (отдельно).** Обновление venv при изменении `uv.lock` — ручным шагом по GO или
   отдельным PR в deploy-агент; чистка системного Python — вне этой задачи.
