# Ray Integration — ROMA Execution Bridge

## Overview

ROMA supports Ray as a distributed execution backend for ML workloads, complementing Slurm and local modes.

Ray uses its **Job Submission API** via the Ray Dashboard HTTP interface.

## Architecture

```
ROMA API  ──► ray_plugin.py ──► HTTP ──► Ray Dashboard (:8265)
                                              │
                                         Ray Head Node
                                         ┌──────┼──────┐
                                        GPU0   GPU1   GPU2
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `RAY_ENABLED` | `false` | Enable Ray backend |
| `RAY_ADDRESS` | `http://127.0.0.1:8265` | Ray Dashboard URL |
| `RAY_HEAD_NODE` | (empty) | SSH host for remote setup (optional) |

## API

### POST /submit with `backend: "ray"`

```bash
curl -X POST https://roma-execution-bridge-asurdev.zocomputer.io/submit \
  -H "X-API-Key: roma-demo-key-2026" \
  -H "Content-Type: application/json" \
  -d '{
    "task": "print(\"Hello from Ray\")",
    "backend": "ray",
    "gpu_required": true
  }'
```

**Response:**
```json
{
  "status": "submitted",
  "job_id": "uuid...",
  "ray_job_id": "raysubmit_..."
}
```

### GET /ray/status/{ray_job_id}

```bash
curl https://roma-execution-bridge-asurdev.zocomputer.io/ray/status/raysubmit_abc123 \
  -H "X-API-Key: roma-demo-key-2026"
```

**Response:**
```json
{
  "ray_job_id": "raysubmit_abc123",
  "status": "SUCCEEDED",
  "message": "Job finished successfully."
}
```

### POST /ray/cancel/{ray_job_id}

```bash
curl -X POST https://roma-execution-bridge-asurdev.zocomputer.io/ray/cancel/raysubmit_abc123 \
  -H "X-API-Key: roma-demo-key-2026"
```

## Error Codes

| Code | Meaning |
|------|---------|
| 400 | Backend disabled (set `RAY_ENABLED=true`) |
| 503 | Ray Dashboard unreachable |
| 500 | Job submission failed |

## Comparison: Slurm vs Ray

| Feature | Slurm | Ray |
|---------|-------|-----|
| **Protocol** | SSH (`sbatch`) | HTTP (Dashboard API) |
| **GPU support** | `--gres=gpu:N` | `runtime_env: {gpu: N}` |
| **Scaling** | Batch (HPC) | Elastic (cloud-native) |
| **Status tracking** | `sacct` / `squeue` | `/api/jobs/{id}` |
| **Best for** | Traditional HPC, universities | ML pipelines, distributed training |
| **Cancel** | `scancel` | `/api/jobs/{id}/stop` |
| **Files** | Shell scripts (.sh) | Python scripts / functions |
