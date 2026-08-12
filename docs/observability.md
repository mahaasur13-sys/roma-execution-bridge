# ROMA Observability — Tracing & Alerts

## OpenTelemetry / Jaeger

ROMA uses OpenTelemetry with Jaeger exporter for distributed tracing.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `JAEGER_ENABLED` | `false` | Enable Jaeger tracing |
| `JAEGER_AGENT_HOST` | `localhost` | Jaeger agent host |
| `JAEGER_AGENT_PORT` | `6831` | Jaeger agent UDP port |
| `JAEGER_SERVICE_NAME` | `roma-execution-bridge` | Service name in traces |

### Tracing Coverage

All FastAPI endpoints are auto-instrumented. Additionally, the following operations have custom spans:

| Span | Attributes | File |
|------|-----------|------|
| `submit_task` | `tenant_id`, `gpu_required`, `job_id`, `status` | `main.py` |

### Viewing Traces

1. Start Jaeger: `docker run -p 16686:16686 -p 6831:6831/udp jaegertracing/all-in-one:latest`
2. Set `JAEGER_ENABLED=true`
3. Send requests to ROMA
4. Open Jaeger UI: `http://localhost:16686`

---

## Prometheus Alerts

Alert rules: `deploy/prometheus/alerts.yml`

| Alert | Condition | Severity |
|-------|-----------|:------:|
| `HighRequestRate` | `rate(roma_requests_total[5m]) > 1000` | Warning |
| `HighActiveJobs` | `roma_jobs_active > 50` | Warning |
| `HighErrorRate` | Error rate > 5% | Critical |

### Telegram Alerting

Alerts are routed to Telegram via Alertmanager webhook. See `docs/architecture.md#alerts` for setup.

---

## Logging

Structured JSON logging (see `docs/metrics.md`). Logs are published to:
- Stdout → `/dev/shm/roma-execution-bridge.log`
- Loki → queryable via Grafana

```bash
tail -f /dev/shm/roma-execution-bridge.log | jq .
```

## Health Dashboard

Grafana dashboard: `https://astrofin-grafana-asurdev.zocomputer.io`
- Panels: TPS, job success rate, active jobs, latency
- Alerts panel: recent firing alerts
