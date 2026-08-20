#!/usr/bin/env python3
"""Generate ROMA API Reference markdown from OpenAPI spec."""

import json

with open("/home/workspace/roma-execution-bridge/docs/openapi.json") as f:
    spec = json.load(f)

paths = spec.get("paths", {})
output = []

output.append("# ROMA Execution Bridge v1.2.0 — API Reference")
output.append("")
output.append(f"**Base URL:** https://roma-execution-bridge-asurdev.zocomputer.io")
output.append(f"**Local:** http://localhost:8900")
output.append(f"**OpenAPI:** 3.1.0 | **Endpoints:** {len(paths)}")
output.append(f"**Docs:** /docs (Swagger UI) | /redoc (ReDoc)")
output.append(f"**Auth:** API key (X-API-Key header) + OAuth2 (Google/GitHub)")
output.append("")

sections = {}
for p, methods in sorted(paths.items()):
    for m, details in methods.items():
        tags = details.get("tags", ["Uncategorized"])
        tag = tags[0] if tags else "Uncategorized"
        sections.setdefault(tag, []).append((p, m.upper(), details))

for tag in sorted(sections):
    output.append(f"## {tag}")
    output.append("")
    for path, method, details in sections[tag]:
        summary = details.get("summary", "—")
        desc = details.get("description", "")
        params = details.get("parameters", [])
        req_body = details.get("requestBody", {})
        responses = details.get("responses", {})
        security = details.get("security", [])

        output.append(f"### `{method} {path}`")
        output.append("")
        output.append(f"**{summary}**")
        if desc:
            output.append(f"> {desc}")
        if security:
            sec_names = [list(s.keys())[0] for s in security]
            output.append(f"🔒 Auth: {', '.join(sec_names)}")
        output.append("")

        if params:
            output.append("| Param | In | Type | Required | Description |")
            output.append("|-------|----|------|----------|-------------|")
            for pr in params:
                nm = pr.get("name", "")
                loc = pr.get("in", "")
                typ = pr.get("schema", {}).get("type", "")
                req = "Yes" if pr.get("required") else "No"
                d = pr.get("description", "")[:100]
                output.append(f"| `{nm}` | {loc} | {typ} | {req} | {d} |")
            output.append("")

        if req_body:
            content = req_body.get("content", {})
            app_json = content.get("application/json", {})
            schema = app_json.get("schema", {})
            title = schema.get("title", "") or schema.get("$ref", "")
            output.append(f"**Body:** JSON (`{title}`)")
            output.append("")

        if responses:
            for code, resp in sorted(responses.items()):
                d = resp.get("description", "")
                output.append(f"- **{code}**: {d[:150]}")
            output.append("")

    output.append("---")
    output.append("")

with open("/home/workspace/roma-execution-bridge/docs/ROMA-API-REFERENCE.md", "w") as f:
    f.write("\n".join(output))

print(f"Written {len(output)} lines to docs/ROMA-API-REFERENCE.md")
