"""Plugin API — FastAPI routes for plugin management.

Endpoints:
  GET    /v1/plugins              — list all registered plugins
  GET    /v1/plugins/:name        — get plugin details
  POST   /v1/plugins/:name/enable — enable a plugin
  POST   /v1/plugins/:name/disable— disable a plugin
  GET    /v1/plugins/marketplace  — list marketplace items
  POST   /v1/plugins/install      — install a plugin from marketplace
  GET    /v1/plugins/traces       — list thought traces
  GET    /v1/plugins/traces/:id   — get a specific trace
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from plugins.core.manager import (
    PluginError,
    PluginNotFoundError,
    get_plugin_manager,
)

router = APIRouter(prefix="/v1/plugins", tags=["plugins"])


# ─── Schemas ───

class PluginSummary(BaseModel):
    name: str
    version: str
    display_name: str
    category: str
    state: str
    minimum_tier: str
    error_message: str | None = None


class PluginDetail(PluginSummary):
    description: str
    author: str
    entry_point: str
    dependencies: list[str]
    permissions: list[str]
    sandbox_policy: str
    tags: list[str]
    config_schema: dict[str, Any]


class EnableRequest(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)


class InstallRequest(BaseModel):
    slug: str
    tenant_id: str


class TraceSummary(BaseModel):
    trace_id: str
    plugin_name: str
    steps_count: int
    total_duration_ms: float
    started_at: str


# ─── Routes ───

@router.get("", response_model=list[PluginSummary])
async def list_plugins(x_api_key: str = Header(..., alias="X-API-Key")):
    """List all registered plugins."""
    mgr = get_plugin_manager()
    return [
        PluginSummary(
            name=p.manifest.name,
            version=p.manifest.version,
            display_name=p.manifest.display_name,
            category=p.manifest.category.value,
            state=p.state.value,
            minimum_tier=p.manifest.minimum_tier.value,
            error_message=p.error_message,
        )
        for p in mgr.list_all()
    ]


@router.get("/{name}", response_model=PluginDetail)
async def get_plugin(name: str, x_api_key: str = Header(..., alias="X-API-Key")):
    """Get detailed plugin info."""
    mgr = get_plugin_manager()
    try:
        p = mgr.get(name)
        return PluginDetail(
            name=p.manifest.name,
            version=p.manifest.version,
            display_name=p.manifest.display_name,
            description=p.manifest.description,
            author=p.manifest.author,
            category=p.manifest.category.value,
            state=p.state.value,
            entry_point=p.manifest.entry_point,
            dependencies=list(p.manifest.dependencies),
            permissions=list(p.manifest.permissions),
            sandbox_policy=p.manifest.sandbox_policy,
            minimum_tier=p.manifest.minimum_tier.value,
            tags=list(p.manifest.tags),
            config_schema=p.manifest.config_schema,
            error_message=p.error_message,
        )
    except PluginNotFoundError:
        raise HTTPException(status_code=404, detail=f"Plugin '{name}' not found")


@router.post("/{name}/enable")
async def enable_plugin(name: str, body: EnableRequest, x_api_key: str = Header(..., alias="X-API-Key")):
    """Enable a plugin with optional config."""
    mgr = get_plugin_manager()
    try:
        await mgr.enable(name, body.config)
        return {"status": "enabled", "plugin": name}
    except PluginNotFoundError:
        raise HTTPException(status_code=404, detail=f"Plugin '{name}' not found")
    except PluginError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{name}/disable")
async def disable_plugin(name: str, x_api_key: str = Header(..., alias="X-API-Key")):
    """Disable a plugin."""
    mgr = get_plugin_manager()
    try:
        await mgr.disable(name)
        return {"status": "disabled", "plugin": name}
    except PluginNotFoundError:
        raise HTTPException(status_code=404, detail=f"Plugin '{name}' not found")
    except PluginError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/marketplace", response_model=list[PluginSummary])
async def list_marketplace(x_api_key: str = Header(..., alias="X-API-Key")):
    """List plugins available in marketplace."""
    mgr = get_plugin_manager()
    return [
        PluginSummary(
            name=p.manifest.name,
            version=p.manifest.version,
            display_name=p.manifest.display_name,
            category=p.manifest.category.value,
            state=p.state.value,
            minimum_tier=p.manifest.minimum_tier.value,
            error_message=p.error_message,
        )
        for p in mgr.list_all()
        if p.manifest.category.value in ("marketplace", "community")
    ]


@router.post("/install")
async def install_plugin(body: InstallRequest, x_api_key: str = Header(..., alias="X-API-Key")):
    """Install a plugin from the marketplace."""
    mgr = get_plugin_manager()
    try:
        plugin = mgr.get(body.slug)
        await mgr.enable(body.slug)
        return {"status": "installed", "plugin": body.slug}
    except PluginNotFoundError:
        raise HTTPException(status_code=404, detail=f"Plugin '{body.slug}' not in marketplace")
    except PluginError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/traces", response_model=list[TraceSummary])
async def list_traces(
    plugin_name: str | None = Query(None),
    x_api_key: str = Header(..., alias="X-API-Key"),
):
    """List thought traces."""
    mgr = get_plugin_manager()
    traces = mgr.list_traces(plugin_name)
    return [
        TraceSummary(
            trace_id=t.trace_id,
            plugin_name=t.plugin_name,
            steps_count=len(t.steps),
            total_duration_ms=t.total_duration_ms,
            started_at=t.started_at.isoformat(),
        )
        for t in traces
    ]


@router.get("/traces/{trace_id}")
async def get_trace(trace_id: str, x_api_key: str = Header(..., alias="X-API-Key")):
    """Get a specific thought trace with all steps."""
    mgr = get_plugin_manager()
    trace = mgr.get_trace(trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail=f"Trace '{trace_id}' not found")

    return {
        "trace_id": trace.trace_id,
        "plugin_name": trace.plugin_name,
        "session_id": trace.session_id,
        "started_at": trace.started_at.isoformat(),
        "finished_at": trace.finished_at.isoformat(),
        "total_duration_ms": trace.total_duration_ms,
        "final_decision": trace.final_decision,
        "steps": [
            {
                "step_id": s.step_id,
                "timestamp": s.timestamp.isoformat(),
                "agent": s.agent,
                "thought": s.thought,
                "data": s.data,
                "confidence": s.confidence,
                "parent_step_id": s.parent_step_id,
            }
            for s in trace.steps
        ],
    }
