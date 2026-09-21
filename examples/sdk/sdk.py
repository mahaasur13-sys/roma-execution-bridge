#!/usr/bin/env python3
"""ROMA Execution Bridge v2.1.0 — Python SDK with billing (GPU-sec + tokens + spend-caps)."""

import os
import requests
import time
import sys

BASE_URL = "http://localhost:8900"
API_KEY = "your-api-key-here"


class ROMAClient:
    """Minimal ROMA client with billing-aware error handling."""

    def __init__(self, base_url: str = BASE_URL, api_key: str = API_KEY):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self._headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base}{path}"
        resp = requests.request(method, url, headers=self._headers, **kwargs)
        if resp.status_code == 402:
            body = resp.json()
            raise SpendCapExceeded(
                body.get("detail", "spend cap exceeded"),
                remaining=body.get("remaining_cap"),
            )
        if resp.status_code == 401:
            raise PermissionError("Invalid API key")
        resp.raise_for_status()
        return resp.json()

    # ── Jobs ──────────────────────────────────────────────────────────

    def submit_job(
        self,
        task: str,
        gpu_required: bool = False,
        gpu_type: str = "any",
        input_tokens: int = 0,
        output_tokens: int = 0,
        plan: str = "free",
        priority: int = 5,
    ) -> dict:
        """Submit a GPU/LLM job with billing metadata."""
        payload = {
            "task": task,
            "gpu_required": gpu_required,
            "gpu_type": gpu_type,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "plan": plan,
            "priority": priority,
        }
        return self._request("POST", "/submit", json=payload)

    def job_status(self, job_id: str) -> dict:
        return self._request("GET", f"/status/{job_id}")

    def complete_job(self, job_id: str) -> dict:
        """Mark job complete — triggers actual billing recalculation."""
        return self._request("POST", f"/complete/{job_id}")

    def list_jobs(self) -> dict:
        return self._request("GET", "/jobs")

    # ── Billing ───────────────────────────────────────────────────────

    def get_usage(self) -> dict:
        """Get tenant usage: GPU-sec, tokens, cost, spend-cap status."""
        return self._request("GET", "/usage")

    def get_balance(self) -> float:
        """Convenience — just the current balance."""
        usage = self.get_usage()
        return usage.get("balance_usd", 0.0)


class SpendCapExceeded(Exception):
    """Raised when tenant exceeds plan spend-cap (HTTP 402)."""

    def __init__(self, message: str, remaining: float | None = None):
        super().__init__(message)
        self.remaining = remaining


# ═══════════════════════════════════════════════════════════════════════
#  Example: Full job lifecycle with billing
# ═══════════════════════════════════════════════════════════════════════


def demo_full_lifecycle():
    client = ROMAClient(api_key=os.environ.get("ROMA_API_KEY", "YOUR_API_KEY"))

    # 1. Check balance before submitting
    usage = client.get_usage()
    print(
        f"Balance: ${usage['balance_usd']:.4f}  "
        f"Plan: {usage['plan']}  "
        f"Spend cap: ${usage['spend_cap_usd']:.2f}  "
        f"GPU used: {usage['total_gpu_seconds']}s  "
        f"Tokens: {usage['total_input_tokens']} in / {usage['total_output_tokens']} out"
    )

    # 2. Submit LLM job with token tracking
    try:
        job = client.submit_job(
            task="Summarize quarterly report (LLM inference)",
            gpu_required=True,
            gpu_type="A100",
            input_tokens=8000,
            output_tokens=2000,
            plan="pro",
        )
        print(
            f"Job created: {job['job_id']}  "
            f"Estimated cost: ${job.get('estimated_cost_usd', 'N/A'):.6f}  "
            f"Remaining cap: ${job.get('spend_cap_remaining', 'N/A'):.6f}"
        )
    except SpendCapExceeded as e:
        print(f"❌ SPEND CAP EXCEEDED: {e}")
        print("   Upgrade plan or wait for billing cycle reset.")
        sys.exit(1)

    # 3. Poll status
    job_id = job["job_id"]
    for _ in range(5):
        status = client.job_status(job_id)
        print(f"  Status: {status['status']}")
        if status["status"] == "completed":
            break
        time.sleep(2)

    # 4. Complete job — triggers actual billing
    completed = client.complete_job(job_id)
    print(f"Completed: {completed}")

    # 5. Check balance after
    usage2 = client.get_usage()
    print(f"Final balance: ${usage2['balance_usd']:.4f}")
    print(f"Charged: ${usage2['balance_usd'] - usage['balance_usd']:.6f}")


if __name__ == "__main__":
    demo_full_lifecycle()
