# ROMA host agent (MVP) — черновик

**Статус:** агент только проходит верификацию (детерминированный matmul-бенчмарк) и шлёт heartbeat. Реальные задачи он **не выполняет** и отклоняет любые задачи, кроме `verify`, пока нет изоляции контейнеров (gVisor/Kata). Агент только сам ходит в ROMA (исходящие запросы), входящих портов не открывает.

**Сервер:** в ветке `feat/host-api-http` (пока не смержена и не задеплоена) в ROMA есть маршруты `POST /hosts/agent/heartbeat`, `/hosts/agent/job`, `/hosts/agent/result`. Они регистрируются только при `ROMA_HOSTS_ENABLED=1`. Пока на продовом сервере ROMA эти маршруты не включены, подключаться агенту некуда.

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
- Токен передаётся только в заголовке `Authorization: Bearer`, идентификатор хоста — в `X-Roma-Host-Id`. Используйте только https-адрес ROMA.
- Каждая задача несёт одноразовый job-токен: повторная отправка результата отклоняется.
- Агент отказывается от задач любого типа, кроме `verify`.
