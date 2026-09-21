"""DecisionOS — Status Transition Guard (root-level, sandbox-safe)."""

TRANSITION_GRAPH = {
    "queued": ["running", "cancelled"],
    "running": ["completed", "failed", "cancelled"],
    "completed": [],
    "failed": ["queued"],
    "cancelled": [],
}


def validate_transition(current_status: str, new_status: str) -> dict:
    """Validate a job status transition. Returns {allowed, reason}."""
    if current_status not in TRANSITION_GRAPH:
        return {"allowed": False, "reason": f"unknown status: {current_status}"}
    allowed_targets = TRANSITION_GRAPH[current_status]
    if new_status in allowed_targets:
        return {"allowed": True, "reason": "ok"}
    return {
        "allowed": False,
        "reason": f"transition_denied: {current_status} → {new_status} invalid",
    }
