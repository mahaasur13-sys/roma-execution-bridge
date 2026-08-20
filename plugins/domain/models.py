"""SQLAlchemy 2.0 ORM models for the Plugin System.

Tables: plugins, plugin_configs, thought_traces, marketplace_listings.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ────────────────────────────────────────
# Plugin Registry
# ────────────────────────────────────────

class PluginModel(Base):
    __tablename__ = "plugins"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(String(1024), default="")
    author: Mapped[str] = mapped_column(String(256), default="ROMA Community")
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="custom", index=True)
    entry_point: Mapped[str] = mapped_column(String(512), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="registered", index=True)
    minimum_tier: Mapped[str] = mapped_column(String(32), nullable=False, default="free")
    sandbox_policy: Mapped[str] = mapped_column(String(32), nullable=False, default="restricted")

    dependencies_json: Mapped[str] = mapped_column(Text, default="[]")
    permissions_json: Mapped[str] = mapped_column(Text, default="[]")
    config_schema_json: Mapped[str] = mapped_column(Text, default="{}")
    tags_json: Mapped[str] = mapped_column(Text, default="[]")

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    loaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    configs: Mapped[list[PluginConfigModel]] = relationship(
        "PluginConfigModel", back_populates="plugin", cascade="all, delete-orphan"
    )

    # ── helpers ──
    def get_dependencies(self) -> list[str]:
        return json.loads(self.dependencies_json)

    def set_dependencies(self, deps: list[str]) -> None:
        self.dependencies_json = json.dumps(deps)

    def get_permissions(self) -> list[str]:
        return json.loads(self.permissions_json)

    def get_tags(self) -> list[str]:
        return json.loads(self.tags_json)


# ────────────────────────────────────────
# Plugin Configs (per-tenant overrides)
# ────────────────────────────────────────

class PluginConfigModel(Base):
    __tablename__ = "plugin_configs"
    __table_args__ = (
        UniqueConstraint("plugin_id", "tenant_id", name="uq_plugin_tenant_config"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plugin_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plugins.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    plugin: Mapped[PluginModel] = relationship("PluginModel", back_populates="configs")

    def get_config(self) -> dict[str, Any]:
        return json.loads(self.config_json)

    def set_config(self, cfg: dict[str, Any]) -> None:
        self.config_json = json.dumps(cfg)


# ────────────────────────────────────────
# Thought Traces
# ────────────────────────────────────────

class ThoughtTraceModel(Base):
    __tablename__ = "thought_traces"
    __table_args__ = (
        Index("idx_traces_plugin_created", "plugin_name", "created_at"),
        Index("idx_traces_session", "session_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, index=True)
    plugin_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    steps_json: Mapped[str] = mapped_column(Text, default="[]")
    final_decision_json: Mapped[str] = mapped_column(Text, default="{}")
    total_duration_ms: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    def get_steps(self) -> list[dict[str, Any]]:
        return json.loads(self.steps_json)

    def get_final_decision(self) -> dict[str, Any]:
        return json.loads(self.final_decision_json)


# ────────────────────────────────────────
# Marketplace Listings
# ────────────────────────────────────────

class MarketplaceListingModel(Base):
    __tablename__ = "marketplace_listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    plugin_name: Mapped[str] = mapped_column(
        String(128), ForeignKey("plugins.name", ondelete="CASCADE"), nullable=False, index=True
    )
    downloads: Mapped[int] = mapped_column(Integer, default=0)
    rating: Mapped[float] = mapped_column(Float, default=0.0)
    ratings_count: Mapped[int] = mapped_column(Integer, default=0)
    installed_count: Mapped[int] = mapped_column(Integer, default=0)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    featured: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
