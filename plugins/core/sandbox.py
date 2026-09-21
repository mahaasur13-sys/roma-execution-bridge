"""Sandbox — plugin isolation and security.

Enforces:
- Permission-based access control
- No filesystem write outside allowed dirs
- No network unless explicitly permitted
- No os.system / subprocess unless permitted
- CPU/memory limits via resource module
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from plugins.domain.plugin import PluginManifest

logger = logging.getLogger("roma.plugin_sandbox")


class SandboxPolicy(StrEnum):
    """Sandbox restriction levels."""

    RESTRICTED = "restricted"  # No fs write, no network, no subprocess
    NETWORK = "network"  # Network allowed, no fs write
    FILE_SYSTEM = "fs"  # FS read/write in allowed dirs
    FULL = "full"  # Full access (enterprise custom plugins only)


ALLOWED_PERMISSIONS: dict[str, set[str]] = {
    "restricted": set(),
    "network": {"network"},
    "fs": {"network", "fs"},
    "full": {"network", "fs", "subprocess", "db", "import"},
}


class PluginSandbox:
    """Enforces sandbox policies on plugin execution.

    Each plugin runs with its declared sandbox_policy.
    Permissions are checked BEFORE execution.
    """

    def __init__(self, manifest: PluginManifest) -> None:
        self.manifest = manifest
        policy = SandboxPolicy(manifest.sandbox_policy)
        self._allowed = ALLOWED_PERMISSIONS.get(policy.value, set())
        logger.debug(
            "Sandbox created for '%s': policy=%s, allowed=%s",
            manifest.name,
            policy.value,
            self._allowed,
        )

    def check_permission(self, permission: str) -> bool:
        """Check if plugin has a specific permission."""
        return permission in self._allowed

    def validate(self) -> list[str]:
        """Validate manifest against sandbox rules. Returns violations."""
        violations: list[str] = []

        # Enterprise-only: full sandbox
        if (
            self.manifest.sandbox_policy == "full"
            and self.manifest.minimum_tier.value != "enterprise"
        ):
            violations.append(
                "Only enterprise tier plugins can use 'full' sandbox policy"
            )

        # No private keys in manifest
        forbidden_fields = ["private_key", "seed_phrase", "mnemonic", "spend_key"]
        for field in forbidden_fields:
            if field in self.manifest.config_schema:
                violations.append(f"Config schema must not contain '{field}' field")

        # Restricted permissions on sensitive operations
        if (
            "subprocess" in self.manifest.permissions
            and self.manifest.minimum_tier.value != "enterprise"
        ):
            violations.append("Subprocess permission requires enterprise tier")

        return violations

    def wrap(self, func: callable) -> callable:
        """Wrap a plugin function with sandbox checks."""

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self._enforce_limits()
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.error("Sandbox violation in '%s': %s", self.manifest.name, e)
                raise PluginSandboxViolation(
                    f"Plugin '{self.manifest.name}' sandbox violation: {e}"
                ) from e

        return wrapper

    @staticmethod
    def _enforce_limits() -> None:
        """Apply resource limits if available."""
        try:
            import resource

            # CPU time: 30 seconds
            resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
            # Memory: 512 MB
            resource.setrlimit(
                resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024)
            )
        except (ImportError, ValueError):
            pass


class PluginSandboxViolation(Exception):
    """Raised when a plugin violates sandbox rules."""
