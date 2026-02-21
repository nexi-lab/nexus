"""Auth brick manifest (Issue #1399).

Extends :class:`~nexus.contracts.brick_manifest.BrickManifest` with
auth-specific configuration and module declarations.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from nexus.contracts.brick_manifest import BrickManifest


@dataclass(frozen=True)
class AuthBrickManifest(BrickManifest):
    """Brick manifest for the Auth module."""

    name: str = "auth"
    protocol: str = "AuthBrickProtocol"
    config_schema: dict[str, dict[str, object]] = field(
        default_factory=lambda: {
            "auth_type": {
                "type": "str",
                "enum": ["static", "database", "local", "oidc", "multi-oidc"],
            },
            "cache_ttl_seconds": {"type": "int", "default": 900},
            "cache_max_size": {"type": "int", "default": 1000},
        }
    )
    required_modules: tuple[str, ...] = (
        "nexus.auth.types",
        "nexus.auth.protocol",
        "nexus.auth.constants",
        "nexus.auth.cache",
        "nexus.auth.providers.base",
        "nexus.auth.providers.discriminator",
    )
    optional_modules: tuple[str, ...] = (
        "nexus.auth.providers.oidc",
        "nexus.auth.providers.database_key",
        "nexus.auth.service",
    )


def verify_imports() -> dict[str, bool]:
    """Convenience wrapper — instantiates manifest and verifies imports."""
    return AuthBrickManifest().verify_imports()
