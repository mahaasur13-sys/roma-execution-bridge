# Slurm Integration — ROMA Execution Bridge

## Overview

ROMA supports submitting jobs to **Slurm HPC clusters** for real GPU execution on physical hardware (e.g., `home-cluster-iac`).

When `SLURM_ENABLED=true`, jobs are dispatched via SSH to the Slurm head node. When `false`, ROMA falls back to local emulation mode.

## Architecture

```
ROMA API → scheduler/slurm_plugin.py → SSH → Slurm Head Node
                                              ├── sbatch (submit)
                                              ├── sacct (query status)
                                              └── scancel (cancel)
```

## Configuration

Set these environment variables in `.env`:

```bash
SLURM_ENABLED=true
SLURM_HEAD_NODE=10.20.20.10
SLURM_USER=ubuntu
SLURM_SSH_KEY=/home/workspace/.ssh/slurm_key
```

| Variable | Description | Default |
|----------|-------------|---------|
| `SLURM_ENABLED` | Enable Slurm integration | `false` |
| `SLURM_HEAD_NODE` | Slurm controller IP/hostname | — |
| `SLURM_USER` | SSH username | `root` |
| `SLURM_SSH_KEY` | Path to SSH private key | `~/.ssh/id_rsa` |

## SSH Key Setup

```bash
# Generate a key (if needed)
ssh-keygen -t ed25519 -f ~/.ssh/slurm_key -C "roma-slurm"

# Copy to Slurm head node
ssh-copy-id -i ~/.ssh/slurm_key.pub ubuntu@10.20.20.10

# Test connectivity
ssh -i ~/.ssh/slurm_key ubuntu@10.20.20.10 sinfo
```

## Job Execution Flow

1. User sends `POST /submit` (or `POST /demo/{name}`)
2. ROMA creates the job record in SQLite
3. If `SLURM_ENABLED`:
   - `SlurmPlugin.execute(job)` generates a `.sh` script from `job.script`
   - Script is piped via SSH to `sbatch` on the Slurm head node
   - Slurm job ID is stored in `jobs.slurm_job_id`
4. Status is polled via `sacct -j <id> --format=State -n`

## sbatch Script Template

For a job with `gpu_count=2`, `memory_mb=16000`, `time_limit=60`:

```bash
#!/bin/bash
#SBATCH --job-name=roma-<uuid>
#SBATCH --gres=gpu:2
#SBATCH --mem=16000
#SBATCH --time=60
#SBATCH --output=/tmp/roma-job-%j.out
#SBATCH --error=/tmp/roma-job-%j.err

# User script
python train.py --epochs 10
```

## API Endpoints

### GET /slurm/status/{slurm_job_id} (protected)

Query Slurm job status directly via `sacct`.

```bash
curl -H "X-API-Key: roma-admin-key" \
  https://roma-execution-bridge-asurdev.zocomputer.io/slurm/status/12345
```

Response:
```json
{
  "slurm_job_id": "12345",
  "status": "RUNNING",
  "raw": "JobID|JobName|Partition|Account|AllocCPUS|State|ExitCode\n12345|roma-xxx|gpu|root|4|RUNNING|0:0"
}
```

### POST /slurm/cancel/{slurm_job_id} (protected)

Cancel a Slurm job directly.

```bash
curl -X POST -H "X-API-Key: roma-admin-key" \
  https://roma-execution-bridge-asurdev.zocomputer.io/slurm/cancel/12345
```

## Slurm Job States

| Slurm State | ROMA Mapping | Description |
|-------------|-------------|-------------|
| `PENDING` | `pending` | Job queued, waiting for resources |
| `RUNNING` | `running` | Job executing |
| `COMPLETED` | `completed` | Job finished successfully |
| `FAILED` | `failed` | Job exited with non-zero code |
| `CANCELLED` | `cancelled` | Job was cancelled by user or admin |
| `TIMEOUT` | `failed` | Job exceeded time limit |

## Graceful Degradation

If `SLURM_ENABLED=false` or the Slurm node is unreachable:

- `POST /submit` → job executes in local emulation mode (status: `running` → `completed`)
- `GET /slurm/status/{id}` → `503 Service Unavailable: Slurm is not enabled`
- `POST /slurm/cancel/{id}` → `503 Service Unavailable: Slurm is not enabled`

## Security

- SSH keys stored outside the repository (configured via `SLURM_SSH_KEY` env var)
- Slurm endpoints require API key authentication
- No Slurm credentials in source code

## Troubleshooting

### "Connection refused" on sbatch
```bash
# Check SSH connectivity
ssh -i $SLURM_SSH_KEY $SLURM_USER@$SLURM_HEAD_NODE hostname

# Check Slurm is running
ssh -i $SLURM_SSH_KEY $SLURM_USER@$SLURM_HEAD_NODE sinfo
```

### "Permission denied" on SSH
```bash
# Verify key permissions
chmod 600 $SLURM_SSH_KEY

# Verify key is authorized on head node
ssh -i $SLURM_SSH_KEY $SLURM_USER@$SLURM_HEAD_NODE echo ok
```

### Slurm returns "Invalid account or partition"
Ensure the Slurm user has access to GPU partitions:
```bash
ssh -i $SLURM_SSH_KEY $SLURM_USER@$SLURM_HEAD_NODE sacctmgr show associations user=$SLURM_USER
```
