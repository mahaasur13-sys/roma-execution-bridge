# Secret Inventory — RaaS-11 (redacted)

**Режим:** READ-ONLY. Значения НЕ выводились и НЕ сохранялись.
**Запрещено в этой сессии:** ротация, отзыв, удаление секретов, переписывание git-истории, `cat`/`env`/`printenv`, сканеры, раскрывающие значение.

---

## 1. Файлы и источники (путь · тип · источник)

### Env-примеры (только плейсхолдеры, реальных значений нет)
| Путь | Тип секрета | Источник |
|------|-------------|----------|
| `.env.example` | API-ключи: CloudPayments (`public_id`/`api_secret`/`webhook_secret`), DeepSeek, SendGrid, Vast.ai, GitHub OAuth (`client_id`/`client_secret`), Google OAuth (`client_id`/`client_secret`) | env-переменные |
| `.env.example` | секреты/токены: `JWT_SECRET`, `ROMA_API_KEY`, `ROMA_DEMO_API_KEY`, `ROMA_GPU_WORKER_TOKEN`, `ALERT_TELEGRAM_BOT_TOKEN`, `ADMIN_API_KEYS` | env-переменные |
| `.env.example` | инфра/БД: `PG_DSN`, `REDIS_URL`, `MINIO_ROOT_PASSWORD`, `ALERT_EMAIL_SMTP_PASSWORD`, `SLURM_SSH_KEY`, `ZO_SSH_KEY` | env-переменные |
| `.env.crypto.example` | крипто-платежи: `CRYPTO_NOWPAYMENTS_API_KEY`/`IPN_SECRET`, `CRYPTO_HELEKET_API_KEY`, `CRYPTO_CRYPTOCLOUD_API_KEY`, `CRYPTO_MONERO_VIEW_KEY` | env-переменные |

### Sealed secrets (значения зашифрованы; имена ключей видны)
| Путь | Тип | Источник |
|------|-----|----------|
| `deploy/values/sealed-secrets/api-sealed.yaml` | `roma_api_key`, `roma_webhook_secret` | SealedSecret `encryptedData` |
| `deploy/values/sealed-secrets/database-sealed.yaml` | `password`, `connection_string` | SealedSecret |
| `deploy/values/sealed-secrets/redis-sealed.yaml` | `password` | SealedSecret |
| `deploy/values/sealed-secrets/stripe-sealed.yaml` | `secret_key`, `webhook_secret` | SealedSecret |

### K8s / Helm секреты
| Путь | Тип | Источник |
|------|-----|----------|
| `deploy/k8s/secrets/roma-secrets.yaml` | K8s Secret (`data`/`stringData`) | манифест |
| `charts/roma-execution-bridge/templates/secrets.yaml` | Helm Secret template (плейсхолдеры `{{ }}`) | шаблон |

### Инструменты шифрования / сканирования / документы
| Путь | Тип | Источник |
|------|-----|----------|
| `.sops.yaml` | конфиг SOPS (`creation_rules` + `age`) — механизм шифрования секретов в git | конфиг |
| `.gitleaks.toml` | правила сканера секретов | конфиг |
| `.gitleaks-baseline.json` | базлайн сканера (исторические находки) | конфиг |
| `deploy/stripe-webhook/scripts/encrypt-secret.sh` | скрипт шифрования webhook-секрета | скрипт |
| `deploy/sealed-secrets/Chart.yaml` | sealed-secrets helm chart | чарт |
| `docs/tutorials/01-submit-job-with-tokens.md` | туториал (использование токена/API-ключа) | документ |

---

## 2. Шаги замены в secret manager + отзыв (ПОДГОТОВЛЕНО, НЕ выполнять)

1. Для каждого секрета зафиксировать владельца и потребителей (сервисы/воркеры, читающие значение).
2. Создать **новое** значение в secret manager (SOPS+age для файлов; sealed-secrets для кластера) — не перезаписывать старое.
3. Обновить потребителей на новое значение и сделать rollout (deploy значений).
4. Прогнать smoke-проверку работоспособности (только после rollout).
5. **Только после подтверждения** — отозвать старое значение у провайдера (CloudPayments / Stripe / Vast.ai / NOWPayments / …).
6. Зафиксировать факт ротации (audit/журнал).

## 3. No-op гарантии этой сессии
- Значения секретов **не выводились и не сохранялись** (только пути/имена ключей/типы).
- Ротация/отзыв/удаление/переписывание истории **не выполнялись**.
- live, DSN, jobs, ставка `0.00001` — не тронуты.
