# ROMA CI/CD Pipeline

## Overview

ROMA использует GitHub Actions для автоматического тестирования и деплоя.

## Pipeline

```
Push to master → CI (test + lint + helm) → Deploy to Zo → Health check
```

## Workflows

### ci.yml — Continuous Integration

**Triggers:** `push` / `pull_request` в `master`/`main`

| Job | Description | Timeout |
|-----|-------------|:------:|
| `test` | Python 3.11, pip install, compile check, pytest | 10 min |
| `lint` | Ruff linter | 2 min |
| `helm-validate` | Helm lint + template + HPA/PDB validation | 3 min |

### deploy.yml — Deploy to Zo

**Triggers:** после успешного CI, или `workflow_dispatch`

| Step | Description |
|------|-------------|
| Setup SSH | `ZO_SSH_KEY` secret |
| Deploy | `git pull` → restart service |
| Health check | `curl /health` |

## GitHub Secrets

| Secret | Description |
|--------|-------------|
| `ZO_HOST` | Zo server hostname |
| `ZO_USER` | SSH username |
| `ZO_SSH_KEY` | Private SSH key |

## Local Deploy

```bash
# Deploy without restart
./deploy.sh

# Deploy + restart service
./deploy.sh --restart
```

## Adding New Tests

1. Create test in `tests/` directory
2. Add to `test_ci.py` or create standalone pytest file
3. CI picks it up automatically

## Troubleshooting

| Problem | Solution |
|---------|----------|
| CI fails on `pip install` | Check `pyproject.toml` dependencies |
| Deploy SSH fails | Verify `ZO_SSH_KEY` in GitHub Secrets |
| Health check fails after deploy | Check service logs: `tail -f /dev/shm/roma-execution-bridge.log` |
