# ROMA v1.2.0 — Observability Import Guide

## Что у вас есть

```
deploy/monitoring/grafana/
├── roma-api-overview.json          # API: RPS, latency p50/p95/p99, error rate, status codes
├── roma-billing-cloudpayments.json # CloudPayments: checkouts, webhooks, success/failure
├── roma-tenants-usage.json         # Тенанты и использование
├── roma-errors-security.json       # Ошибки, auth failures, rate-limit hits
└── roma-backends-queue.json        # Бэкенды и очередь

deploy/monitoring/
└── alert-rules-roma.yml            # 6 алерт-правил Grafana Unified Alerting
```

---

## Шаг 1: Импорт дашбордов в Grafana Cloud

### Через UI (рекомендуется)

1. Откройте https://mahaasur13.grafana.net
2. **Dashboards → New → Import**
3. Загрузите JSON-файл (drag & drop или copy-paste)
4. Выберите datasource: `GrafanaCloud-Prom` (Prometheus)
5. Нажмите **Import**
6. Повторите для всех 5 файлов

### Через API

```bash
GRAFANA_URL="https://mahaasur13.grafana.net"
API_KEY="${GRAFANA_API_KEY}"  # Settings → Service Accounts → API Keys

for f in deploy/monitoring/grafana/*.json; do
  curl -X POST "$GRAFANA_URL/api/dashboards/db" \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"dashboard\":$(cat $f),\"overwrite\":true}"
  echo "Imported: $f"
done
```

---

## Шаг 2: Настройка алертов

### Вариант A — Через UI

1. Grafana → **Alerting → Alert rules → New alert rule**
2. Выбрать datasource: `GrafanaCloud-Prom`
3. Вставить PromQL из `alert-rules-roma.yml` в поле **Expression**
4. Настроить:
   - **For**: согласно правилу (2m, 3m или 5m)
   - **Labels**: `severity`, `category`
   - **Annotations**: `summary`, `description`

### Вариант B — Через Terraform / Grafana Provider

```hcl
resource "grafana_rule_group" "roma" {
  name             = "roma-critical"
  folder_uid       = "roma-alerts"
  interval_seconds = 60

  rule {
    name           = "ROMAHighErrorRate"
    for            = "5m"
    condition      = "C"

    data {
      ref_id = "A"
      relative_time_range { from = 300, to = 0 }
      datasource_uid = "grafanacloud-prom"
      model = jsonencode({
        expr = "sum(rate(roma_requests_total{status=~\"5..\"}[5m])) / sum(rate(roma_requests_total[5m])) > 0.05"
      })
    }
  }
}
```

### Правила алертов (6 штук)

| Alert | Severity | For | PromQL |
|-------|----------|:---:|--------|
| **ROMAHighErrorRate** | critical | 5m | `sum(rate(roma_requests_total{status=~"5..\"}[5m])) / sum(rate(roma_requests_total[5m])) > 0.05` |
| **ROMABillingWebhookDown** | critical | 3m | `sum(increase(roma_cloudpayments_failure_total[10m])) > 0 or absent(roma_cloudpayments_webhooks_total)` |
| **ROMANoMetrics** | critical | 2m | `roma_requests_total or absent(roma_requests_total)` |
| **ROMAHighLatency** | warning | 5m | `histogram_quantile(0.95, sum(rate(roma_request_duration_seconds_bucket[5m])) by (le)) > 1.0` |
| **ROMADeepQueue** | warning | 3m | `roma_queue_depth > 10` |
| **ROMAAuthSpike** | warning | 5m | `rate(roma_auth_failures_total[5m]) > 0.2` |

---

## Шаг 3: Настройка Contact Points

### Telegram (основной)

1. Grafana → **Alerting → Contact points → New contact point**
2. Name: `telegram-alerts`
3. Integration: **Telegram**
4. Bot Token: ваш токен бота
5. Chat ID: ваш chat ID

### Дополнительные каналы

| Канал | Когда использовать |
|-------|-------------------|
| **Email** | weekly digest, не-critical |
| **Discord webhook** | dev-team канал |
| **PagerDuty** | on-call ротация (production) |

---

## Шаг 4: Notification Policies

1. Grafana → **Alerting → Notification policies**
2. Default contact point: `telegram-alerts`
3. Добавьте routing:
   - `severity = critical` → `telegram-alerts` (немедленно)
   - `severity = warning` → `telegram-alerts` (группировать каждые 5 мин)
   - `category = billing` → `telegram-alerts` + email

---

## Шаг 5: Проверка после импорта

```bash
# 1. Дашборды видны в Grafana
open https://mahaasur13.grafana.net/dashboards

# 2. Метрики отображаются (данные есть)
curl -s https://roma-execution-bridge-asurdev.zocomputer.io/metrics | grep roma_requests_total

# 3. Алерты в состоянии Normal (не firing)
# Alerting → Alert rules → фильтр "ROMA" → все зеленые

# 4. Тестовый алерт (опционально)
# Временное правило: roma_requests_total >= 0 → warning
# Убедиться, что уведомление пришло в Telegram
```

---

## Дашборды — что где

| Дашборд | UID | Ключевые панели |
|---------|-----|----------------|
| **ROMA API Overview** | `roma-api-overview` | RPS, p50/p95/p99, error rate %, status codes, uptime |
| **ROMA Billing & CloudPayments** | `roma-billing-v2` | Checkout rate, webhooks by event, success/failure |
| **ROMA Tenants & Usage** | `roma-tenants-v1` | Active tenants, jobs/tenant, plan distribution |
| **ROMA Errors & Security** | `roma-errors-v1` | Errors by endpoint, auth failures, 4xx/5xx breakdown |
| **ROMA Backends & Queue** | `roma-backends-v1` | Queue depth, workers, backends status |

---

## Быстрые ссылки

- [Grafana Cloud](https://mahaasur13.grafana.net)
- [ROMA /metrics](https://roma-execution-bridge-asurdev.zocomputer.io/metrics)
- [ROMA /health](https://roma-execution-bridge-asurdev.zocomputer.io/health)
- [Alert Rules YAML](https://github.com/mahaasur13-sys/roma-execution-bridge/blob/master/deploy/monitoring/alert-rules-roma.yml)
