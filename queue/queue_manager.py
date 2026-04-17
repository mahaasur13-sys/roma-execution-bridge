"""
ROMA Queue Manager — Redis-backed job queue with priority and TTL.
"""

import json
import uuid
import time
from typing import Optional, List
from enum import Enum
from dataclasses import dataclass, asdict
from datetime import datetime


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ROMAJob:
    job_id: str
    task: str
    plan: dict
    status: JobStatus
    priority: int = 5  # 1=highest, 10=lowest
    created_at: float = None
    started_at: float = None
    finished_at: float = None
    error: str = None
    gpu_node: str = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = time.time()

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> "ROMAJob":
        d = json.loads(data)
        d["status"] = JobStatus(d["status"])
        return cls(**d)

    def age_seconds(self) -> float:
        return time.time() - self.created_at


class QueueManager:
    """
    Redis-backed FIFO + priority queue for ROMA jobs.
    Uses two sorted sets: pending queue (by priority+timestamp)
    and running set (by start time).
    """

    def __init__(self, redis_client):
        self.r = redis_client
        self.PENDING_KEY = "roma:queue:pending"
        self.RUNNING_KEY = "roma:queue:running"
        self.JOBS_HASH = "roma:jobs"
        self.GPU_LOCK_KEY = "roma:gpu:lock"
        self.MAX_CONCURRENT = 1  # RTX 3060 — single GPU

    def enqueue(self, plan: dict, task: str, priority: int = 5) -> str:
        job_id = f"roma-{uuid.uuid4().hex[:12]}"
        job = ROMAJob(
            job_id=job_id,
            task=task,
            plan=plan,
            status=JobStatus.PENDING,
            priority=priority,
        )
        # Store job data
        self.r.hset(self.JOBS_HASH, job_id, job.to_json())
        # Add to pending sorted set: score = priority * 1e12 + timestamp
        score = priority * 1e12 + job.created_at
        self.r.zadd(self.PENDING_KEY, {job_id: score})
        return job_id

    def dequeue(self) -> Optional[ROMAJob]:
        """Pop highest-priority pending job if GPU is available."""
        # Check GPU lock
        if self._is_gpu_locked():
            return None
        # Pop highest priority job
        result = self.r.zpopmin(self.PENDING_KEY, count=1)
        if not result:
            return None
        job_id = result[0][0].decode()
        job_data = self.r.hget(self.JOBS_HASH, job_id)
        if not job_data:
            return None
        job = ROMAJob.from_json(job_data.decode())
        job.status = JobStatus.RUNNING
        job.started_at = time.time()
        # Update job
        self.r.hset(self.JOBS_HASH, job_id, job.to_json())
        # Move to running set
        self.r.zadd(self.RUNNING_KEY, {job_id: job.started_at})
        # Acquire GPU lock
        self.r.set(self.GPU_LOCK_KEY, job_id, ex=3600)
        return job

    def complete(self, job_id: str, success: bool = True, error: str = None):
        job_data = self.r.hget(self.JOBS_HASH, job_id)
        if not job_data:
            return
        job = ROMAJob.from_json(job_data.decode())
        job.finished_at = time.time()
        job.status = JobStatus.SUCCEEDED if success else JobStatus.FAILED
        job.error = error
        self.r.hset(self.JOBS_HASH, job_id, job.to_json())
        # Remove from running
        self.r.zrem(self.RUNNING_KEY, job_id)
        # Release GPU lock
        self.r.delete(self.GPU_LOCK_KEY)

    def cancel(self, job_id: str):
        job_data = self.r.hget(self.JOBS_HASH, job_id)
        if job_data:
            job = ROMAJob.from_json(job_data.decode())
            job.status = JobStatus.CANCELLED
            job.finished_at = time.time()
            self.r.hset(self.JOBS_HASH, job_id, job.to_json())
        self.r.zrem(self.PENDING_KEY, job_id)
        self.r.zrem(self.RUNNING_KEY, job_id)
        self.r.delete(self.GPU_LOCK_KEY)

    def get_status(self, job_id: str) -> Optional[ROMAJob]:
        data = self.r.hget(self.JOBS_HASH, job_id)
        return ROMAJob.from_json(data.decode()) if data else None

    def list_pending(self) -> List[ROMAJob]:
        job_ids = self.r.zrange(self.PENDING_KEY, 0, -1)
        return [self.get_status(j.decode()) for j in job_ids]

    def list_running(self) -> List[ROMAJob]:
        job_ids = self.r.zrange(self.RUNNING_KEY, 0, -1)
        return [self.get_status(j.decode()) for j in job_ids]

    def _is_gpu_locked(self) -> bool:
        return self.r.exists(self.GPU_LOCK_KEY) == 1

    @property
    def queue_depth(self) -> int:
        return self.r.zcard(self.PENDING_KEY)

    @property
    def is_gpu_busy(self) -> bool:
        return self._is_gpu_locked()