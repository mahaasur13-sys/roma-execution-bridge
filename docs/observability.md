# ROMA Observability — Tracing & Alerts

## OpenTelemetry / OTLP

ROMA exports OpenTelemetry traces over OTLP/gRPC. Tracing is opt-in and is enabled only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | OTLP/gRPC collector endpoint, e.g. `http://127.0.0.1:4317`. Unset = tracing disabled |
| `OTEL_SERVICE_NAME` | `roma-execution-bridge` | Service name in traces |

If the OTLP exporter packages are missing (`opentelemetry-exporter-otlp-proto-grpc`), the service logs one warning and starts without tracing.

`JAEGER_ENABLED` is deprecated: setting it only logs a warning, nothing else happens.

### Tracing Coverage

All FastAPI endpoints are auto-instrumented. Additionally, the following operations have custom spans:

| Span | Attributes | File |
|------|-----------|------|
| `submit_task` | `tenant_id`, `gpu_required`, `job_id`, `status` | `main.py` |

### Viewing Traces

1. Start a collector or Jaeger (Jaeger accepts OTLP natively on `4317`): `docker run -p 16686:16686 -p 4317:4317 jaegertracing/all-in-one:latest`
2. Set `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4317`
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
