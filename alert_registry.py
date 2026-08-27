#!/usr/bin/env python3
"""Minimal file-backed alert registry for ROMA.

Provides a queryable source of "open alerts" for the escalation protocol
(alerts that do not resolve within N minutes trigger escalation to a senior
engineer). Intended as a lightweight baseline until a full ELK/Loki
aggregation is deployed.

Behaviour:
  - Each alert is appended to ``data/alerts.jsonl`` (append-only audit log).
  - ``record_alert`` de-duplicates via an optional ``dedup_key`` (an open alert
    with the same key is refreshed instead of duplicated) to avoid alert storms.
  - Alerts are also dispatched through ``alerts.AlertDispatcher`` (silent unless
    a channel is configured in env).

Lifecycle: open -> acknowledged -> resolved.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from alerts import AlertDispatcher, Alert, AlertLevel
except Exception:  # allow standalone use without the alerts package
    AlertDispatcher = Alert = AlertLevel = None  # type: ignore

_DATA_DIR = Path(__file__).parent / "data"
_ALERTS_FILE = _DATA_DIR / "alerts.jsonl"

# Non-resolved alerts (open + acknowledged), keyed by alert_id.
_OPEN: dict[str, dict] = {}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_dir() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)


def _dispatch(level: str, title: str, body: str) -> None:
    if AlertDispatcher is None:
        return
    try:
        lvl = getattr(AlertLevel, level.upper(), AlertLevel.WARNING)
        AlertDispatcher().send(Alert(level=lvl, title=title, body=body))
    except Exception:
        pass


def _load() -> None:
    """Reconstruct open alerts from the append-only log (last-write-wins per id)."""
    if not _ALERTS_FILE.exists():
        return
    try:
        with _ALERTS_FILE.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("status") in ("open", "acknowledged"):
                    _OPEN[rec["alert_id"]] = rec
    except Exception:
        pass


def record_alert(
    level: str,
    title: str,
    body: str,
    source: str = "",
    dedup_key: str | None = None,
) -> str:
    """Record an alert as open (de-duplicating on ``dedup_key``). Returns alert_id."""
    _ensure_dir()
    now = _now()

    if dedup_key:
        for rec in _OPEN.values():
            if rec.get("dedup_key") == dedup_key:
                rec["last_seen"] = now
                rec["body"] = body
                return rec["alert_id"]

    alert_id = f"alt-{int(time.time() * 1000)}-{os.urandom(3).hex()}"
    rec = {
        "alert_id": alert_id,
        "level": level,
        "title": title,
        "body": body,
        "source": source,
        "dedup_key": dedup_key,
        "status": "open",
        "opened_at": now,
        "last_seen": now,
        "acknowledged_at": None,
        "resolved_at": None,
    }
    _OPEN[alert_id] = rec
    try:
        with _ALERTS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    _dispatch(level, title, body)
    return alert_id


def ack_alert(alert_id: str) -> bool:
    """Mark an open alert as acknowledged. Returns False if not open."""
    rec = _OPEN.get(alert_id)
    if not rec:
        return False
    rec["status"] = "acknowledged"
    rec["acknowledged_at"] = _now()
    return True


def resolve_alert(alert_id: str) -> bool:
    """Resolve (close) an open/acknowledged alert. Returns False if not found."""
    rec = _OPEN.pop(alert_id, None)
    if not rec:
        return False
    rec["status"] = "resolved"
    rec["resolved_at"] = _now()
    try:
        with _ALERTS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return True


def list_open_alerts() -> list[dict]:
    """Return non-resolved alerts (open + acknowledged)."""
    return list(_OPEN.values())


def open_count() -> int:
    return len(_OPEN)


# Load any previously-persisted open alerts on import.
_load()
