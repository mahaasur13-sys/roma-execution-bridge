"""
ROMA Execution Bridge – FastAPI + Pydantic v2
In-memory storage (will be SQLite later).
"""

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, ConfigDict

# ============================================
# API KEY AUTH
# ============================================

API_KEYS_FILE = Path(__file__).parent / "config" / "api_keys.json"

def _load_api_keys() -> set[str]:
    with open(API_KEYS_FILE) as f:
        data = json.load(f)
    return set(data.get("keys", []))

VALID_API_KEYS: set[str] = _load_api_keys()

def verify_api_key(x_api_key: str = Header(None)) -> str:
    if not x_api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing X-API-Key header. Request a key at https://roma-execution-bridge-asurdev.zocomputer.io",
        )
    if x_api_key not in VALID_API_KEYS:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key

# ============================================
# МОДЕЛИ
# ============================================

class RomaTaskInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_default=True,
    )

    task: str = Field(..., min_length=1)
    gpu_required: bool = Field(default=False)
    priority: int = Field(default=5, ge=1, le=10)
    execution_mode: str = Field(default="k8s_job")


class RomaTaskResponse(BaseModel):
    status: str
    job_id: str
    roma_dispatch: dict
    dag: list
    estimated_resources: dict
    gpu_required: bool


class RomaStatusResponse(BaseModel):
    job_id: str
    status: str
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None


# ============================================
# ПРИЛОЖЕНИЕ
# ============================================

app = FastAPI(
    title="ROMA Execution Platform",
    version="1.0.0",
)

# ============================================
# IN-MEMORY STORAGE (временное)
# ============================================

jobs: dict = {}
queue_depth: int = 0


# ============================================
# ЭНДПОИНТЫ
# ============================================

@app.post("/submit", response_model=RomaTaskResponse, status_code=202, dependencies=[Depends(verify_api_key)])
async def submit_task(payload: RomaTaskInput):
    global queue_depth

    try:
        job_id = str(uuid.uuid4())
        queue_depth += 1

        job = {
            "status": "queued",
            "job_id": job_id,
            "rom": f"rom://local/{job_id}",
            "submitted_at": datetime.utcnow().isoformat(),
            "payload": payload.model_dump(),
        }

        jobs[job_id] = job
        queue_depth -= 1

        return RomaTaskResponse(
            status="queued",
            job_id=job_id,
            roma_dispatch={
                "protocol": "rom",
                "target": job["rom"],
            },
            dag=["validate", "dispatch", "execute", "commit"],
            estimated_resources={
                "cpu_cores": 2,
                "memory_mb": 512,
                "gpu": 0,
            },
            gpu_required=payload.gpu_required,
        )

    except Exception as e:
        queue_depth -= 1
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/status/{job_id}", response_model=RomaStatusResponse, dependencies=[Depends(verify_api_key)])
async def get_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]

    return RomaStatusResponse(
        job_id=job_id,
        status=job["status"],
        created_at=job["submitted_at"],
    )


@app.post("/cancel/{job_id}", dependencies=[Depends(verify_api_key)])
async def cancel_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    jobs[job_id]["status"] = "cancelled"

    return {
        "status": "cancelled",
        "job_id": job_id,
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "queue_depth": queue_depth,
        "jobs": len(jobs),
    }


# ============================================
# ТОЧКА ВХОДА
# ============================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8899)


@app.post("/submit/cluster", status_code=202, dependencies=[Depends(verify_api_key)])
async def submit_atom_cluster(payload: dict):
    """Submit job as ATOMCluster managed execution."""
    cluster_spec = payload.get("cluster_spec", {})
    cluster_name = cluster_spec.get("name", "default")

    # Dispatch as managed job
    job_id = str(uuid.uuid4())
    job = {
        "status": "atom_cluster_managed",
        "job_id": job_id,
        "cluster_name": cluster_name,
        "execution_mode": "atom_cluster",
        "atom_cluster": {
            "name": cluster_name,
            "managed": True,
            "nodes": cluster_spec.get("nodes", 1),
        },
    }
    jobs[job_id] = job
    return job


@app.get("/jobs", dependencies=[Depends(verify_api_key)])
async def list_jobs():
    return {
        "rom_version": "1.0.0",
        "queue": len(jobs),
        "jobs": list(jobs.values())[-10:],
        "execution_modes": ["k8s_job", "k8s_persistent", "atom_cluster", "batch"],
    }
