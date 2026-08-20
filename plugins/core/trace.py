"""TraceCollector — thought-trace engine for agent reasoning.

Records every reasoning step from Policy Engine, Decision Gate,
and AI agents. Enables full auditability and replay.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from plugins.domain.plugin import ThoughtStep, ThoughtTrace

logger = logging.getLogger("roma.trace_collector")


class TraceNotFoundError(Exception):
    """Trace ID not found."""


class TraceCollector:
    """Collects and stores thought traces.

    In-memory for runtime. Persists to DB on finish.
    """

    def __init__(self) -> None:
        self._active: dict[str, list[ThoughtStep]] = {}
        self._completed: dict[str, ThoughtTrace] = {}
        self._metadata: dict[str, dict[str, Any]] = {}

    def start(self, plugin_name: str, session_id: str) -> str:
        """Begin a new trace. Returns trace_id."""
        trace_id = str(uuid.uuid4())
        self._active[trace_id] = []
        self._metadata[trace_id] = {
            "plugin_name": plugin_name,
            "session_id": session_id,
            "started_at": datetime.now(timezone.utc),
        }
        logger.debug("Trace started: %s (plugin=%s)", trace_id, plugin_name)
        return trace_id

    def add_step(
        self,
        trace_id: str,
        thought: str,
        *,
        data: dict[str, Any],
        confidence: float = 1.0,
    ) -> ThoughtStep:
        """Add a reasoning step."""
        if trace_id not in self._active:
            raise TraceNotFoundError(f"Trace '{trace_id}' not active")

        parent_id = None
        steps = self._active[trace_id]
        if steps:
            parent_id = steps[-1].step_id

        step = ThoughtStep(
            step_id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc),
            agent=self._metadata[trace_id]["plugin_name"],
            thought=thought,
            data=data,
            confidence=confidence,
            parent_step_id=parent_id,
        )
        steps.append(step)
        return step

    def finish(self, trace_id: str, final_decision: dict[str, Any]) -> ThoughtTrace:
        """Finish a trace, persist it, return the record."""
        if trace_id not in self._active:
            raise TraceNotFoundError(f"Trace '{trace_id}' not active")

        meta = self._metadata[trace_id]
        steps = self._active.pop(trace_id)
        started_at: datetime = meta["started_at"]
        finished_at = datetime.now(timezone.utc)

        total_ms = (finished_at - started_at).total_seconds() * 1000

        trace = ThoughtTrace(
            trace_id=trace_id,
            plugin_name=meta["plugin_name"],
            session_id=meta["session_id"],
            started_at=started_at,
            finished_at=finished_at,
            steps=tuple(steps),
            final_decision=final_decision,
            total_duration_ms=round(total_ms, 2),
        )

        self._completed[trace_id] = trace
        logger.info(
            "Trace finished: %s — %d steps, %.1fms",
            trace_id, len(steps), total_ms,
        )
        return trace

    def get(self, trace_id: str) -> ThoughtTrace | None:
        """Get a completed trace."""
        return self._completed.get(trace_id)

    def list_traces(self, plugin_name: str | None = None) -> list[ThoughtTrace]:
        """List completed traces, optionally filtered by plugin."""
        if plugin_name:
            return [t for t in self._completed.values() if t.plugin_name == plugin_name]
        return list(self._completed.values())

    def active_count(self) -> int:
        return len(self._active)

    def completed_count(self) -> int:
        return len(self._completed)
