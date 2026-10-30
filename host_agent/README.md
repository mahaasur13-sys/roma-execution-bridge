# ROMA host agent (MVP) — черновик

**Статус:** агент только проходит верификацию (детерминированный matmul-бенчмарк) и шлёт heartbeat. Реальные задачи он **не выполняет** и отклоняет любые задачи, кроме `verify`, пока нет изоляции контейнеров (gVisor/Kata). Агент только сам ходит в ROMA (исходящие запросы), входящих портов не открывает.

**Сервер:** в ветке `feat/host-api-http` (пока не смержена и не задеплоена) в ROMA есть маршруты `POST /hosts/agent/heartbeat`, `/hosts/agent/job`, `/hosts/agent/result`. Они регистрируются только при `ROMA_HOSTS_ENABLED=1`. Пока на продовом сервере ROMA эти маршруты не включены, подключаться агенту некуда.

## Поток оператора (сторона ROMA)
Все команды выполняются на сервере ROMA с `ROMA_HOSTS_ENABLED=1`, `ROMA_HOSTS_STORE=pg` и `PG_DSN`. DSN в выводе маскируется.

1. **Регистрация** (статус `pending`). `--owner-label` — несекретная метка (имя). Не указывайте email и телефоны: это правило для оператора, CLI содержимое метки **не проверяет**, и всё введённое попадёт в запись хоста.
   ```bash
   python scripts/roma_hosts_admin.py register --host-id felix-3060 --gpu-model RTX_3060 \
       --gpu-count 1 --vram-gb 12 --owner-label "felix (pop-os)"
   ```
2. **Выдача токена.** CLI сначала создаёт файл `--out` с правами 0600 (существующий файл не перезаписывается), затем выпускает токен и пишет его в этот файл. Хранится только sha256. Если файл создать не удалось, токен не меняется и старый продолжает работать. Повторная выдача заменяет старый токен: он сразу получает 401.
   При запуске только с `--out` токен не попадает в stdout и логи. **Флаг `--print-once` печатает токен открытым текстом в stdout**, в том числе вместе с `--out`: он может остаться в истории терминала, логах CI или записи сессии. Используйте его только при необходимости.
   ```bash
   python scripts/roma_hosts_admin.py issue-token --host-id felix-3060 --out ./felix-3060.token
   ```
3. **Передать файл токена владельцу хоста** защищённым каналом и удалить его у оператора. Владелец кладёт файл в `~/.config/roma/host-token` с правами 600 (см. ниже).
4. **Владелец запускает агент** (раздел «Запуск на хосте»).
5. **Верификация.** Порог TFLOPS берётся из `ROMA_HOSTS_BENCH_THRESHOLDS` или `--thresholds`; агент получит задачу на следующем опросе:
   ```bash
   python scripts/roma_hosts_admin.py start-verification --host-id felix-3060
   ```
6. **Проверка результата:** `python scripts/roma_hosts_admin.py list` — статус `active`, уровень `probation` (у внешнего хоста) или `verified` (у своего, `--own-host`).
7. **Ручное подтверждение доверия** для внешнего хоста: `python scripts/roma_hosts_admin.py approve --host-id felix-3060`.

Прочее:
- `suspend --host-id ... --reason ...` — приостановить хост;
- `revoke-token --host-id ...` — отозвать токен; работающий сервер сразу отвечает агенту 401, рестарт не нужен.

## Запуск на хосте (пример: pop-os, пользователь felix, без root)
pypi.org с этой машины недоступен, поэтому torch и numpy ставятся с индекса PyTorch:

```bash
cd ~
git clone https://github.com/mahaasur13-sys/roma-execution-bridge.git
python3 -m venv ~/roma-agent-venv
~/roma-agent-venv/bin/pip install --index-url https://download.pytorch.org/whl/cu121 torch numpy
```

Токен хоста выдаёт оператор ROMA один раз. Сохраните его в файл, доступный только вам:

```bash
mkdir -p ~/.config/roma && chmod 700 ~/.config/roma
nano ~/.config/roma/host-token          # вставить токен, сохранить
chmod 600 ~/.config/roma/host-token
cat > ~/.config/roma/agent.env <<'CFG'
ROMA_URL=https://<адрес-ROMA>/hosts/agent
ROMA_HOST_ID=<host_id от оператора>
ROMA_HOST_TOKEN_FILE=/home/felix/.config/roma/host-token
ROMA_AGENT_POLL_S=30
ROMA_AGENT_DEVICE=cuda
CFG
```

Проверка без запуска бенчмарка и без отправки результата:

```bash
cd ~/roma-execution-bridge
~/roma-agent-venv/bin/python -m host_agent.agent --config ~/.config/roma/agent.env --dry-run
```

Один цикл (heartbeat → получить задачу → бенчмарк на GPU → отправить результат):

```bash
~/roma-agent-venv/bin/python -m host_agent.agent --config ~/.config/roma/agent.env --once
```

Постоянная работа: та же команда без `--once`. Остановить — Ctrl+C.

Проверить GPU-бенчмарк локально, без ROMA: `~/roma-agent-venv/bin/python scripts/host_bench_selftest.py 512 20`.

## Безопасность
- Токен не пишется в логи и в `repr` конфига. Храните его в файле с правами 600, а не в переменной окружения.
- По умолчанию токен передаётся только в заголовке `Authorization: Bearer`, идентификатор хоста — в `X-Roma-Host-Id`. Используйте только https-адрес ROMA.

## Заголовок токена (`--token-header`)
- `--token-header authorization` (по умолчанию, или `ROMA_TOKEN_HEADER=authorization`) — запросы побайтно как раньше.
- `--token-header x-roma-host-token` (или `ROMA_TOKEN_HEADER=x-roma-host-token`) — токен идёт только в `X-Roma-Host-Token: <token>`, без `Authorization`. Нужен для публичного фронта на Zo (API-роут zo.space): он вырезает `Authorization`, пропуская `X-*`, и сам переводит `X-Roma-Host-Token` обратно в `Authorization: Bearer` перед отправкой в ROMA. Контракт сервера ROMA не меняется: напрямую ROMA принимает только `Authorization`.
- В обоих режимах токен не попадает в логи и в вывод `--dry-run`.

## Прочее
- Каждая задача несёт одноразовый job-токен: повторная отправка результата отклоняется.
- Агент отказывается от задач любого типа, кроме `verify`.

## User-Agent
Агент всегда шлёт `User-Agent: roma-host-agent/<AGENT_VERSION>` (сейчас `roma-host-agent/0.1`), в обоих режимах `--token-header`. Причина: Cloudflare перед zo.space отвечает 403 (не JSON) на стандартный `Python-urllib/3.x`, а агент видел это как `heartbeat rejected code=403 error=bad_response`.
