"""PluginManager — the heart of DecisionOS Plugin System.

Manages the full lifecycle: register → load → enable → disable → unload → update.
Inspired by Cordis (DeepSeek Harness) plugin runtime.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from plugins.domain.plugin import (
    PluginCategory,
    PluginInstance,
    PluginManifest,
    PluginState,
    PluginTier,
    ThoughtStep,
    ThoughtTrace,
)
from plugins.core.trace import TraceCollector

logger = logging.getLogger("roma.plugin_manager")


class PluginError(Exception):
    """Base exception for plugin system errors."""


class PluginNotFoundError(PluginError):
    """Plugin not found in registry."""


class PluginLoadError(PluginError):
    """Failed to load a plugin."""


class PluginSandboxViolation(PluginError):
    """Plugin violated its sandbox policy."""


class PluginDependencyError(PluginError):
    """Missing or circular dependency."""


class PluginManager:
    """Central plugin registry and lifecycle manager.

    Thread-safe. Single instance per process (singleton pattern).

    Architecture:
        ┌──────────────────────────────────────────┐
        │              PluginManager               │
        │  ┌────────┐  ┌────────┐  ┌───────────┐  │
        │  │Registry│  │ Loader │  │ Sandbox   │  │
        │  ├────────┤  ├────────┤  ├───────────┤  │
        │  │Plugins │  │Import  │  │Permissions│  │
        │  │Configs │  │Validate│  │ResourceLim│  │
        │  │State   │  │Resolve │  │Isolation  │  │
        │  └────────┘  └────────┘  └───────────┘  │
        │  ┌─────────────────────────────────────┐ │
        │  │        TraceCollector               │ │
        │  └─────────────────────────────────────┘ │
        └──────────────────────────────────────────┘
    """

    def __init__(self) -> None:
        self._registry: dict[str, PluginInstance] = {}
        self._trace_collector = TraceCollector()
        self._lock = asyncio.Lock()

    # ─── Registry ───

    def get(self, name: str) -> PluginInstance:
        """Get a plugin by name. Raises PluginNotFoundError."""
        plugin = self._registry.get(name)
        if not plugin:
            raise PluginNotFoundError(f"Plugin '{name}' not registered")
        return plugin

    def list_enabled(self) -> list[PluginInstance]:
        """Return all currently enabled plugins."""
        return [p for p in self._registry.values() if p.is_enabled()]

    def get_enabled(self, name: str) -> PluginInstance:
        """Get a plugin, raising if not enabled."""
        plugin = self.get(name)
        if not plugin.is_enabled():
            raise PluginError(f"Plugin '{name}' is not enabled (state={plugin.state.value})")
        return plugin

    def list_all(self) -> list[PluginInstance]:
        return list(self._registry.values())

    def list_by_category(self, category: PluginCategory) -> list[PluginInstance]:
        return [p for p in self._registry.values() if p.manifest.category == category]

    def list_by_tier(self, tier: str) -> list[PluginInstance]:
        return [p for p in self._registry.values() if p.is_available(tier)]

    def exists(self, name: str) -> bool:
        return name in self._registry

    # ─── Lifecycle ───

    async def register(self, manifest: PluginManifest) -> PluginInstance:
        """Register a new plugin. Does not load it yet."""
        async with self._lock:
            if self.exists(manifest.name):
                raise PluginError(f"Plugin '{manifest.name}' already registered")

            instance = PluginInstance(
                id=str(uuid.uuid4()),
                manifest=manifest,
                state=PluginState.REGISTERED,
            )
            self._registry[manifest.name] = instance
            logger.info("Plugin registered: %s v%s", manifest.name, manifest.version)
            return instance

    async def load(self, name: str) -> PluginInstance:
        """Load a plugin: resolve deps, validate, import module."""
        async with self._lock:
            instance = self.get(name)

            if instance.state in (PluginState.LOADED, PluginState.ENABLED):
                return instance

            if instance.state == PluginState.ERROR:
                raise PluginLoadError(f"Cannot load plugin in error state: {name}")

            try:
                instance.state = PluginState.UPDATING

                # Resolve dependencies
                for dep_name in instance.manifest.dependencies:
                    if not self.exists(dep_name):
                        raise PluginDependencyError(
                            f"Plugin '{name}' depends on '{dep_name}' which is not registered"
                        )
                    dep = self._registry[dep_name]
                    if dep.state not in (PluginState.LOADED, PluginState.ENABLED):
                        await self.load(dep_name)

                # Import the entry point
                module_path, class_name = instance.manifest.entry_point.rsplit(":", 1)
                module = importlib.import_module(module_path)
                plugin_class = getattr(module, class_name)

                # Validate plugin interface
                self._validate_plugin_class(plugin_class, instance.manifest)

                instance._instance = plugin_class()
                instance.state = PluginState.LOADED
                instance.loaded_at = datetime.now(timezone.utc)

                logger.info("Plugin loaded: %s", name)
                return instance

            except Exception as exc:
                instance.state = PluginState.ERROR
                instance.error_message = str(exc)
                logger.error("Failed to load plugin '%s': %s", name, exc)
                raise PluginLoadError(f"Failed to load plugin '{name}': {exc}") from exc

    async def enable(self, name: str, config: dict[str, Any] | None = None) -> PluginInstance:
        """Enable a loaded plugin with optional config."""
        instance = await self.load(name)

        async with self._lock:
            if instance.state == PluginState.ENABLED:
                return instance

            try:
                if config:
                    instance.config = config

                # Call plugin's on_enable if it exists
                if instance._instance and hasattr(instance._instance, "on_enable"):
                    if inspect.iscoroutinefunction(instance._instance.on_enable):
                        await instance._instance.on_enable(instance.config)
                    else:
                        instance._instance.on_enable(instance.config)

                instance.state = PluginState.ENABLED
                instance.enabled_at = datetime.now(timezone.utc)
                logger.info("Plugin enabled: %s", name)
                return instance

            except Exception as exc:
                instance.state = PluginState.ERROR
                instance.error_message = str(exc)
                logger.error("Failed to enable plugin '%s': %s", name, exc)
                raise PluginError(f"Failed to enable plugin '{name}': {exc}") from exc

    async def disable(self, name: str) -> PluginInstance:
        """Disable a plugin (keeps it loaded)."""
        async with self._lock:
            instance = self.get(name)

            if instance.state != PluginState.ENABLED:
                return instance

            try:
                if instance._instance and hasattr(instance._instance, "on_disable"):
                    if inspect.iscoroutinefunction(instance._instance.on_disable):
                        await instance._instance.on_disable()
                    else:
                        instance._instance.on_disable()

                instance.state = PluginState.DISABLED
                logger.info("Plugin disabled: %s", name)
                return instance

            except Exception as exc:
                instance.state = PluginState.ERROR
                instance.error_message = str(exc)
                logger.error("Failed to disable plugin '%s': %s", name, exc)
                raise PluginError(f"Failed to disable plugin '{name}': {exc}") from exc

    async def unload(self, name: str) -> PluginInstance:
        """Unload a plugin completely."""
        instance = self.get(name)

        async with self._lock:
            if instance.state == PluginState.ENABLED:
                await self.disable(name)

            # Check no other plugin depends on this one
            for other_name, other in self._registry.items():
                if other_name != name and name in other.manifest.dependencies:
                    if other.state in (PluginState.LOADED, PluginState.ENABLED):
                        raise PluginDependencyError(
                            f"Cannot unload '{name}': '{other_name}' depends on it"
                        )

            instance._instance = None
            instance.state = PluginState.UNLOADED
            logger.info("Plugin unloaded: %s", name)
            return instance

    async def update(self, name: str, new_manifest: PluginManifest) -> PluginInstance:
        """Update a plugin to a new version."""
        async with self._lock:
            instance = self.get(name)
            was_enabled = instance.state == PluginState.ENABLED

            if was_enabled:
                await self.disable(name)

            instance.state = PluginState.UPDATING
            instance._instance = None

            # Replace manifest
            instance.manifest = new_manifest
            instance.error_message = None

            await self.load(name)

            if was_enabled:
                await self.enable(name)

            logger.info("Plugin updated: %s → v%s", name, new_manifest.version)
            return instance

    # ─── Thought Tracing ───

    def start_trace(self, plugin_name: str, session_id: str) -> str:
        """Begin a new thought trace. Returns trace_id."""
        return self._trace_collector.start(plugin_name, session_id)

    def add_thought(
        self,
        trace_id: str,
        thought: str,
        *,
        data: dict[str, Any] | None = None,
        confidence: float = 1.0,
    ) -> ThoughtStep:
        """Add a reasoning step to an active trace."""
        return self._trace_collector.add_step(trace_id, thought, data=data or {}, confidence=confidence)

    def finish_trace(self, trace_id: str, final_decision: dict[str, Any]) -> ThoughtTrace:
        """Finish a trace and return the complete record."""
        return self._trace_collector.finish(trace_id, final_decision)

    def get_trace(self, trace_id: str) -> ThoughtTrace | None:
        return self._trace_collector.get(trace_id)

    def list_traces(self, plugin_name: str | None = None) -> list[ThoughtTrace]:
        return self._trace_collector.list_traces(plugin_name)

    # ─── Bulk Operations ───

    async def load_all(self, names: list[str]) -> list[PluginInstance]:
        results = await asyncio.gather(*(self.load(n) for n in names), return_exceptions=True)
        return [
            r if not isinstance(r, Exception) else PluginInstance(
                manifest=PluginManifest(name="error", entry_point=":Error"),
                state=PluginState.ERROR, error_message=str(r)
            )
            for r in results
        ]

    async def enable_all(self, names: list[str]) -> list[PluginInstance]:
        return [await self.enable(n) for n in names]

    # ─── Internals ───

    @staticmethod
    def _validate_plugin_class(cls: type, manifest: PluginManifest) -> None:
        """Validate that a plugin class has the required interface."""
        if not inspect.isclass(cls):
            raise PluginLoadError(f"Entry point '{manifest.entry_point}' is not a class")

        # Check for minimum required methods
        required_methods = ["run"]
        for method_name in required_methods:
            if not hasattr(cls, method_name):
                logger.warning(
                    "Plugin '%s' does not implement '%s()'. May not be callable.",
                    manifest.name, method_name,
                )


# ─── Singleton ───

_global_manager: PluginManager | None = None


def get_plugin_manager() -> PluginManager:
    """Get the global PluginManager singleton."""
    global _global_manager
    if _global_manager is None:
        _global_manager = PluginManager()
    return _global_manager
