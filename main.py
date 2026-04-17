"""
ROMA Execution Bridge — FastAPI + OpenAPI strict mode
Pydantic v2 models, no extra fields, no fallback generation
"""

import json
import uuid
import traceback
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict

# =============================================================================
# STRICT SCHEMA — No additional properties allowed
# =============================================================================

class RomaTaskInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",          # REJECT extra fields
        validate_default=True
    )

    task: str = Field(..., min_length=1, description="User task (MANDATORY)")
    gpu_required: bool = Field(default=False)
    priority: int = Field(default=5, ge=1, le=10)
    execution_mode: str = Field(default="k8s_job")

class RomaRejectResponse(BaseModel):
    status: str = "rejected"
    error: dict
    roma_validation: dict
    behavior: str = "exit_immediately"

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
    k8s_job_name: Optional[str] = None
    node_name: Optional[str] = None
    error: Optional[str] = None

# =============================================================================
# APP — Execution Bridge
# =============================================================================

app = FastAPI(
    title="ROMA Execution Platform",
    version="1.0.0",
    description="Deterministic execution kernel — NOT a generative AI. Strict input contract.",
    docs_url="/docs",
    redoc_url="/redoc"
)

# In-memory state (production: Redis + PostgreSQL)
jobs = {}
queue_depth = 0

@app.post("/submit", response_model=RomaTaskResponse, status_code=202)
async def submit_task(payload: RomaTaskInput):
    """Submit ROMA task — strict validation, no fallback generation."""
    job_id = str(uuid.uuid4())
    global queue_depth
    queue_depth += 1

    job = {
        "status": "queued",
        "job_id": job_id,
        "rom