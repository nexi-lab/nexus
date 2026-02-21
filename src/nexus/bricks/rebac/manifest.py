"""ReBAC brick manifest (Issue #1385).

Extends :class:`~nexus.contracts.brick_manifest.BrickManifest` with
ReBAC-specific configuration and module declarations.
"""

from dataclasses import dataclass, field

from nexus.contracts.brick_manifest import BrickManifest


@dataclass(frozen=True)
class ReBACBrickManifest(BrickManifest):
    """Brick manifest for the ReBAC module."""

    name: str = "rebac"
    protocol: str = "ReBACBrickProtocol"
    config_schema: dict[str, dict[str, object]] = field(
        default_factory=lambda: {
            "enforce_zone_isolation": {"type": "bool", "default": True},
            "enable_graph_limits": {"type": "bool", "default": True},
            "enable_leopard": {"type": "bool", "default": True},
            "enable_tiger_cache": {"type": "bool", "default": True},
        }
    )
    required_modules: tuple[str, ...] = (
        "nexus.bricks.rebac.manager",
        "nexus.bricks.rebac.types",
        "nexus.bricks.rebac.graph",
        "nexus.bricks.rebac.cache",
        "nexus.bricks.rebac.tuples",
    )
    optional_modules: tuple[str, ...] = ("nexus.bricks.rebac.cache.tiger",)


def verify_imports() -> dict[str, bool]:
    """Convenience wrapper — instantiates manifest and verifies imports."""
    return ReBACBrickManifest().verify_imports()
