"""FastAPI request/response models for the ROMA app (extracted from main.py).

These are the Pydantic v2 schemas used by the HTTP routes in ``main.py``.
They live here so the app module stays focused on wiring (middleware, router
includes, startup/shutdown), while the schemas remain importable without
importing the full app.

Kept verbatim from main.py — no field/validation changes (A1).
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class RomaTaskInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_default=True,
    )
    task: str = Field(..., min_length=1)
    image: Optional[str] = Field(default=None, max_length=300)
    gpu_required: bool = Field(default=False)
    priority: int = Field(default=5, ge=1, le=10)
    execution_mode: str = Field(default="k8s_job")
    backend: Optional[str] = Field(default=None, pattern="^(local|slurm|ray|tensordock|vastai|runpod|gpu_worker|aws_ec2)$")
    instance_type: str = Field(default="any", description="GPU type: any, RTX 3060, A100, H100")


class RomaTaskResponse(BaseModel):
    status: str
    job_id: str
    roma_dispatch: dict
    dag: list
    estimated_resources: dict
    gpu_required: bool
    tenant_id: str


class RomaStatusResponse(BaseModel):
    job_id: str
    status: str
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None
    backend: Optional[str] = None
    backend_job_id: Optional[str] = None


class CheckoutRequest(BaseModel):
    plan: str = Field(default="pro", pattern="^(free|pro|enterprise)$")


class CheckoutResponse(BaseModel):
    url: str
    plan: str
    mode: str


class UsageResponse(BaseModel):
    tenant_id: str
    plan: str
    usage: dict
    limits: dict


class ChatMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str = Field(..., min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=10)
    name: Optional[str] = Field(default=None, max_length=80)


class TestAlertRequest(BaseModel):
    """Запрос на тестовую отправку алерта."""
    channel: str | None = None  # telegram, discord, email или None = все
    message: str = "🧪 Тестовый алерт ROMA Execution Bridge v2.1.0"
