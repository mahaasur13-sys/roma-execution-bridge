"""DecisionOS — Audit Events (root-level, sandbox-safe)."""
import uuid
import db_adapter as db

def write_audit_event(tenant_id: str, event_type: str, entity_type: str,
                       entity_id: str, data: dict | None = None) -> dict:
    """Write a single audit event to PG. Returns {id, ...}."""
    eid = str(uuid.uuid4())
    return db.insert_audit_event(eid, tenant_id, event_type, entity_type, entity_id, data or {})
