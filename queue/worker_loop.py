"""
ROMA Worker Loop — async worker that pulls from queue and dispatches to bridge.
"""

import asyncio
import time
import signal
import sys
from typing import Optional

from queue.queue_manager import QueueManager, JobStatus


class WorkerLoop:
    """
    Async worker that:
    1. Polls queue (FIFO by priority)
    2. Dispatches jobs to Execution Bridge
    3. Updates job status on completion
    """

    def __init__(
        self,
        queue_manager: QueueManager,
        bridge_url: str = "http://localhost:8080",
        poll_interval: float = 2.0,
        job_timeout: float = 3600.0,
    ):
        self.queue = queue_manager
        self.bridge_url = bridge_url
        self.poll_interval = poll_interval
        self.job_timeout = job_timeout
        self.running = False

    async def dispatch_job(self, job) -> bool:
        """Send job to bridge, return True on success."""
        import aiohttp
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    f"{self.bridge_url}/submit",
                    json=job.plan,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        return result.get("status") == "submitted"
                    return False
        except Exception:
            return False

    async def poll_and_dispatch(self):
        """Main worker loop — poll queue, dispatch jobs, handle timeouts."""
        while self.running:
            # Try to dequeue
            job = self.queue.dequeue()
            if job is None:
                await asyncio.sleep(self.poll_interval)
                continue

            print(f"[WORKER] Dispatching {job.job_id}: {job.task[:60]}...")

            # Check timeout
            if job.started_at and (time.time() - job.started_at > self.job_timeout):
                self.queue.complete(job.job_id, success=False, error="timeout")
                print(f"[WORKER] {job.job_id} timed out")
                continue

            # Dispatch async
            success = await self.dispatch_job(job)

            if not success:
                self.queue.complete(job.job_id, success=False, error="dispatch_failed")
                print(f"[WORKER] {job.job_id} dispatch failed")
            else:
                print(f"[WORKER] {job.job_id} dispatched successfully")

            await asyncio.sleep(1)

    def start(self):
        """Start the worker loop."""
        self.running = True
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.poll_and_dispatch())
        finally:
            loop.close()

    def stop(self):
        self.running = False