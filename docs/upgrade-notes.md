# ROMA — Upgrade Notes

## SSH deploy key — требования формата

Класс `G-ZO-DEPLOY-RED` («Error loading key: error in libcrypto») — это отказ
загрузки приватного SSH-ключа на GitHub-раннере (`ubuntu-latest`, OpenSSH ≥9).
Причина — **формат/кодировка** секрета `ZO_SSH_KEY` (CRLF-мусор при вставке,
PuTTY `.ppk`, старый PEM RSA, или лишние пробелы/переводы строк).

### Требования к ключу

- Формат: **OpenSSH** (`-----BEGIN OPENSSH PRIVATE KEY-----`), желательно `ed25519`.
- Без passphrase (CI не интерактивен).
- Без CRLF, без пробелов вокруг строк, без хвостового мусора.

### Генерация (с нуля)

```bash
ssh-keygen -t ed25519 -N "" -C "roma-deploy-zo" -f ~/.ssh/roma_zo_deploy
# публичная часть: ~/.ssh/roma_zo_deploy.pub  → добавить в ~/.ssh/authorized_keys на Zo
```

### Конвертация старого RSA PEM

```bash
ssh-keygen -p -N "" -m PEM -f ~/.ssh/roma_zo_deploy_rsa   # при необходимости снять passphrase
ssh-keygen -p -N "" -f ~/.ssh/roma_zo_deploy_rsa           # перевести в OpenSSH-формат
```

### Конвертация PuTTY `.ppk`

```bash
# PuTTYgen: Conversions → Export OpenSSH key
# или командой:
puttygen roma_zo_deploy.ppk -O private-openssh -o roma_zo_deploy
```

### Проверка ПЕРЕД заливкой

```bash
ssh-keygen -y -f <путь-к-приватному-ключу> >/dev/null && echo "OK: загружается" || echo "FAIL: не загружается"
```

### Куда залить

GitHub → repo `mahaasur13-sys/roma-execution-bridge` → **Settings → Secrets and variables → Actions** →
секрет **`ZO_SSH_KEY`** (значение = весь приватный ключ, включая строки `-----BEGIN…`/`-----END…`,
без лишних пробелов и пустых строк). Также убедиться: `ZO_USER`, `ZO_HOST` заданы.

### Проверка после заливки

Рельсы `deploy.yml` теперь fail-fast: шаг «Setup SSH key» валидирует ключ
(`ssh-keygen -y`) до `ssh-add` и печатает класс проблемы (диагностический шаг).
Следующий штатный `deploy` (по merge в master) покажет `key: OpenSSH` + зелёный SSH-шаг.
