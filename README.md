# ROMA — Private Cloud OS CLI

Human interface layer for your Private Cloud OS (Kubernetes + GPU + Docker).

## Quick Start

```bash
# 1. Start the Execution Bridge
cd roma-execution-bridge
uvicorn main:app --host 0.0.0.0 --port 8080 &

# 2. Submit a task
python roma_cli.py run "train YOLOv8 on RTX3060"

# 3. Check status
python roma_cli.py status <job_id>

# 4. Stream logs
python roma_cli.py logs <job_id>

# 5. List jobs
python roma_cli.py list
```

## Architecture

```
User: roma run "train model"
   │
   ├─ ROMA CLI (Typer)
   │     └─ LLM API → ROMA JSON plan
   │
   └─ Execution Bridge (FastAPI)
         ├─ Security Gate
         ├─ GPU Scheduler (RTX 3060 safe mode)
         └─ JSON → K8s Job compiler
```

## Commands

| Command | Description |
|---------|-------------|
| `roma run "task"` | Submit natural language task |
| `roma status <id>` | Check job status |
| `roma logs <id>` | Stream job logs |
| `roma list` | List recent jobs |
| `roma delete <id>` | Delete a job |
| `roma health` | Check bridge health |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ROMA_BRIDGE_HOST` | `http://localhost:8080` | Bridge API URL |
| `ZO_CLIENT_IDENTITY_TOKEN` | from env | LLM API auth token |
