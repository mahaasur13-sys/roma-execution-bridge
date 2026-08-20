#!/usr/bin/env python3
"""DecisionOS — Generate enhanced OpenAPI schema (Week 4)."""
import json
import sys

try:
    from main import app
except Exception as e:
    print(f"Error loading main.app: {e}")
    sys.exit(1)

schema = app.openapi()

# Add DecisionOS branding
schema["info"]["title"] = "DecisionOS API v1"
schema["info"]["version"] = "1.0.0"
schema["info"]["description"] = (
    "DecisionOS — Enterprise Platform for Managed Decision & Execution.\n\n"
    "Authentication: Header `X-API-Key: <your-key>`\n\n"
    "Supported endpoints:\n"
    "- `/submit` — Submit job (backward compatible)\n"
    "- `/v1/decisions` — Decision create/list/evaluate\n"
    "- `/v1/jobs/{id}/...` — Job retry/cancel/complete/worker-ack\n"
    "- `/status/{id}` — Job status\n"
    "- `/jobs` — List jobs\n"
    "- `/usage` — Tenant usage metrics\n"
    "- `/billing/create-checkout-session` — Stripe checkout\n"
    "- `/health` — Health & PG status\n"
    "- `/api/chat/stream` — AI Assistant with tool calling"
)

# Add security scheme
schema["components"] = schema.get("components", {})
schema["components"]["securitySchemes"] = {
    "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"}
}
schema["security"] = [{"ApiKeyAuth": []}]

# Add tags
tag_order = [
    {"name": "decisions", "description": "Decision lifecycle: create, read, evaluate"},
    {"name": "jobs", "description": "Execution jobs: retry, cancel, complete, worker acknowledgement"},
    {"name": "execution", "description": "Job submit, status, listing (backward compatible)"},
    {"name": "billing", "description": "Usage and Stripe billing"},
    {"name": "health", "description": "System health and connectivity checks"},
    {"name": "ai", "description": "AI assistant with tool calling"},
]
existing_tags = {t["name"] for t in schema.get("tags", [])}
for tag in tag_order:
    if tag["name"] not in existing_tags:
        schema.setdefault("tags", []).append(tag)

open("openapi.json", "w").write(json.dumps(schema, indent=2, ensure_ascii=False))
print("✅ openapi.json generated")
