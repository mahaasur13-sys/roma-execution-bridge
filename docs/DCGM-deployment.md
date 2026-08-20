# DCGM Deployment Guide

> Автоматическое развёртывание NVIDIA DCGM Exporter на GPU-нодах K8s.

## Требования

- K8s кластер с GPU-нодами (`kubectl get nodes -l nvidia.com/gpu=true` — не пусто)
- NVIDIA GPU Operator или как минимум драйверы + Container Toolkit
- `kubectl` доступен и настроен

## Быстрый старт

```bash
chmod +x deploy/scripts/deploy_dcgm.sh
./deploy/scripts/deploy_dcgm.sh
```

Скрипт автоматически:
1. Проверит доступность `kubectl`
2. Найдёт GPU-ноды в кластере
3. Применит DaemonSet из `deploy/manifests/dcgm-exporter.yaml`
4. Дождётся готовности всех подов (timeout 120 секунд)
5. Проверит появление GPU-метрик в Prometheus

## Ручное развёртывание

```bash
# 1. Проверить наличие GPU-нод
kubectl get nodes -l nvidia.com/gpu=true

# 2. Применить DaemonSet
kubectl apply -f deploy/manifests/dcgm-exporter.yaml

# 3. Проверить поды
kubectl get pods -n monitoring -l app.kubernetes.io/name=nvidia-dcgm-exporter

# 4. Проверить метрики в Prometheus
curl -s 'http://localhost:9090/api/v1/query?query=DCGM_FI_DEV_GPU_UTIL' | jq '.data.result | length'
```

## Проверки после развёртывания

- [ ] Все поды DCGM Exporter в статусе Running
- [ ] Prometheus видит таргеты DCGM (Targets → monitoring/dcgm-exporter)
- [ ] Метрики доступны: `DCGM_FI_DEV_GPU_UTIL`, `DCGM_FI_DEV_FB_USED`, `DCGM_FI_DEV_FB_FREE`
- [ ] GPU-дашборд импортирован: `deploy/monitoring/grafana/grafana-gpu-dashboard.json`
- [ ] GPU-статус в `/health`: `gpu_available=true`

## Устранение неполадок

| Симптом | Проверка |
|---------|---------|
| Поды в Pending | `kubectl describe pod -n monitoring -l app.kubernetes.io/name=nvidia-dcgm-exporter` |
| Нет метрик | `kubectl logs -n monitoring -l app.kubernetes.io/name=nvidia-dcgm-exporter` |
| Prometheus не видит | Проверить ServiceMonitor: `kubectl get servicemonitor -n monitoring dcgm-exporter` |

## Архитектура

```
GPU Node ──► DCGM Exporter (DaemonSet) ──► Prometheus (scrape) ──► Grafana Dashboard
```
