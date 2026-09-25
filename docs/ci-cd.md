# ROMA CI/CD Pipeline

## Overview

ROMA использует GitHub Actions для автоматического тестирования и деплоя.

## Pipeline

```
Push to master → CI (test + lint + helm) → машина Zo сама тянет master → вердикт `deploy/zo`
```

## Workflows

### ci.yml — Continuous Integration

**Triggers:** `push` / `pull_request` в `master`/`main`

| Job | Description | Timeout |
|-----|-------------|:------:|
| `test` | Python 3.11, pip install, compile check, pytest | 10 min |
| `lint` | Ruff linter | 2 min |
| `helm-validate` | Helm lint + template + HPA/PDB validation | 3 min |

### deploy.yml — Deploy to Zo (pull-модель, вердикт)

**Triggers:** после успешного CI, или `workflow_dispatch`

Раннер **не ходит** на машину Zo (у него нет до неё DNS/маршрута — `Temporary failure in name
resolution`, exit 255). Выкат делает сама машина: Zo user service `zo-deploy-agent`
(канон `deploy/ops/zo_deploy_agent.py`) тянет `master`, когда CI по этому SHA зелёный, применяет
миграции, рестартит `roma-execution-bridge` и ставит commit status `deploy/zo`. Этот workflow
читает вердикт по своему SHA.

| Step | Description |
|------|-------------|
| Gate | устаревшие SHA (перекрытые новым master) пропускаются |
| Verdict | ждём commit status `deploy/zo` (15 мин): `success` → зелёный, `failure`/`error` → красный с причиной |
| Health check | `curl /health` публичного адреса |

Секретов для выката не требуется: на машине стоит аутентифицированный `gh` CLI.

## GitHub Secrets

`ZO_HOST` / `ZO_USER` / `ZO_SSH_KEY` — **легаси, не используются**: инбаунд-SSH до машины Zo
с раннера недостижим по построению, выкат идёт pull-моделью (агент на Zo). Эти секреты можно
удалить; новых не требуется.

## Local Deploy

```bash
# Deploy without restart
./deploy.sh

# Deploy + restart service
./deploy.sh --restart
```

## Adding New Tests

1. Create test in `tests/` directory
2. Add to `scripts/ci_smoke.py` or create a standalone pytest file
3. CI picks it up automatically

## Troubleshooting

| Problem | Solution |
|---------|----------|
| CI fails on `pip install` | Check `pyproject.toml` dependencies |
| `Deploy to Zo` красный | Это вердикт машины Zo: смотри `description` commit status `deploy/zo` и `/var/lib/roma-deploy-agent/deploy-agent.log` |
| Health check fails after deploy | Check service logs: `tail -f /dev/shm/roma-execution-bridge.log` |
