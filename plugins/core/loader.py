"""PluginLoader — discovers, imports, and validates plugins."""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path

from plugins.domain.plugin import PluginManifest, PluginCategory, PluginTier

logger = logging.getLogger("roma.plugin_loader")


class PluginLoader:
    """Discovers and imports plugins from file system or packages.

    Discovery strategy:
    1. Scan plugin directories for manifest.json
    2. Validate manifest against PluginManifest schema
    3. Import the entry point module
    4. Return resolved PluginManifest + class reference
    """

    DEFAULT_SEARCH_PATHS: tuple[str, ...] = (
        "plugins/builtin",
        "plugins/examples",
        "plugins/community",
    )

    def __init__(self, search_paths: list[str] | None = None) -> None:
        self._search_paths = search_paths or list(self.DEFAULT_SEARCH_PATHS)

    def discover(self, base_dir: str) -> list[PluginManifest]:
        """Scan all search paths for plugin manifests."""
        manifests: list[PluginManifest] = []

        for search_path in self._search_paths:
            full_path = Path(base_dir) / search_path
            if not full_path.exists():
                continue

            for manifest_file in full_path.rglob("manifest.json"):
                try:
                    manifest = self.load_manifest(str(manifest_file))
                    manifests.append(manifest)
                except Exception as e:
                    logger.warning("Failed to load manifest from %s: %s", manifest_file, e)

        return manifests

    def load_manifest(self, manifest_path: str) -> PluginManifest:
        """Load and validate a manifest.json file."""
        with open(manifest_path) as f:
            data = json.load(f)

        return PluginManifest(
            name=data["name"],
            version=data.get("version", "1.0.0"),
            display_name=data["display_name"],
            description=data.get("description", ""),
            author=data.get("author", "ROMA Community"),
            category=PluginCategory(data.get("category", "custom")),
            entry_point=data["entry_point"],
            dependencies=tuple(data.get("dependencies", [])),
            minimum_tier=PluginTier(data.get("minimum_tier", "free")),
            permissions=tuple(data.get("permissions", [])),
            sandbox_policy=data.get("sandbox_policy", "restricted"),
            tags=tuple(data.get("tags", [])),
            config_schema=data.get("config_schema", {}),
        )

    def import_plugin(self, manifest: PluginManifest) -> type:
        """Import a plugin's entry point class."""
        module_path, class_name = manifest.entry_point.rsplit(":", 1)

        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            raise ImportError(
                f"Failed to import module '{module_path}' for plugin '{manifest.name}': {e}"
            ) from e

        if not hasattr(module, class_name):
            raise AttributeError(
                f"Module '{module_path}' has no class '{class_name}' "
                f"required by plugin '{manifest.name}'"
            )

        plugin_class = getattr(module, class_name)
        return plugin_class

    def validate_permissions(self, manifest: PluginManifest, allowed_permissions: set[str]) -> list[str]:
        """Check requested permissions against allowed set. Returns denials."""
        requested = set(manifest.permissions)
        denied = requested - allowed_permissions
        return list(denied)
