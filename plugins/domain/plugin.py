"""DecisionOS Plugin System — Domain Entities (Pydantic v2).

Inspired by DeepSeek Harness / Cordis.
Every component is a plugin. The core is minimal.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ────────────────────────────────────────
# Plugin Lifecycle State
# ────────────────────────────────────────

class PluginState(StrEnum):
    """Plugin lifecycle states (Cordis-inspired)."""
    REGISTERED = "registered"
    LOADED = "loaded"
    ENABLED = "enabled"
    DISABLED = "disabled"
    UNLOADED = "unloaded"
    ERROR = "error"
    UPDATING = "updating"


class PluginCategory(StrEnum):
    """Plugin categories for the marketplace."""
    POLICY = "policy"
    DECISION = "decision"
    CRYPTO = "crypto"
    WALLET = "wallet"
    SUPPORT = "support"
    UI = "ui"
    DASHBOARD = "dashboard"
    ANALYTICS = "analytics"
    INTEGRATION = "integration"
    CUSTOM = "custom"


class PluginTier(StrEnum):
    """Which plan tiers can see/use this plugin."""
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


# ────────────────────────────────────────
# Plugin Manifest
# ────────────────────────────────────────

class PluginManifest(BaseModel):
    """Plugin manifest — metadata for discovery and loading.

    Analogous to package.json / pyproject.toml for plugins.
    """
    model_config = ConfigDict(frozen=True)

    name: str = Field(..., min_length=1, max_length=128, description="Unique plugin slug (kebab-case)")
    version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+(-[a-zA-Z0-9.]+)?$")
    display_name: str = Field(..., max_length=256)
    description: str = Field(default="", max_length=1024)
    author: str = Field(default="ROMA Community")
    category: PluginCategory = PluginCategory.CUSTOM
    entry_point: str = Field(..., description="Fully qualified Python path: pkg.module:Class")
    dependencies: tuple[str, ...] = Field(default_factory=tuple, description="Other plugin names required")
    minimum_tier: PluginTier = PluginTier.FREE
    config_schema: dict[str, Any] = Field(default_factory=dict, description="JSON Schema for plugin config")
    permissions: tuple[str, ...] = Field(default_factory=tuple, description="Requested permissions: db,network,fs")
    sandbox_policy: str = Field(default="restricted", description="sandbox profile: restricted | network | full")
    tags: tuple[str, ...] = Field(default_factory=tuple)


# ────────────────────────────────────────
# Plugin Instance (runtime)
# ────────────────────────────────────────

class PluginInstance(BaseModel):
    """Runtime plugin instance — wraps a loaded plugin object."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    manifest: PluginManifest
    state: PluginState = PluginState.REGISTERED
    loaded_at: datetime | None = None
    enabled_at: datetime | None = None
    error_message: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    _instance: Any = None  # The actual plugin class/object

    def is_enabled(self) -> bool:
        return self.state == PluginState.ENABLED

    def is_available(self, tier: str) -> bool:
        """Check if plugin is visible to given tier."""
        tier_order = {"free": 0, "pro": 1, "enterprise": 2}
        return tier_order.get(tier, 0) >= tier_order.get(self.manifest.minimum_tier.value, 0)


# ────────────────────────────────────────
# Thought Trace — agent reasoning log
# ────────────────────────────────────────

class ThoughtStep(BaseModel):
    """Single step in an agent's reasoning chain."""
    model_config = ConfigDict(frozen=True)

    step_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    agent: str = Field(..., description="Plugin/agent name")
    thought: str = Field(..., description="Natural language reasoning step")
    data: dict[str, Any] = Field(default_factory=dict, description="Structured data for this step")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    parent_step_id: str | None = None


class ThoughtTrace(BaseModel):
    """Full trace of an agent's reasoning chain.

    Analogous to LangChain trace but PluginManager-owned.
    """
    model_config = ConfigDict(frozen=True)

    trace_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    plugin_name: str
    session_id: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    steps: tuple[ThoughtStep, ...] = Field(default_factory=tuple)
    final_decision: dict[str, Any] = Field(default_factory=dict)
    total_duration_ms: float = 0.0


# ────────────────────────────────────────
# Marketplace Item
# ────────────────────────────────────────

class PluginMarketplaceItem(BaseModel):
    """A plugin listing in the internal marketplace."""
    model_config = ConfigDict(frozen=True)

    slug: str = Field(..., description="Unique marketplace slug")
    manifest: PluginManifest
    downloads: int = 0
    rating: float = Field(default=0.0, ge=0.0, le=5.0)
    ratings_count: int = 0
    installed_count: int = 0
    verified: bool = False
    featured: bool = False
    installed_by_tenant: bool = False
