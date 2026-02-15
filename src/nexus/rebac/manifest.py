"""ReBAC brick manifest and startup validation (Issue #1385).

Declares the ReBAC brick's metadata and provides verify_imports()
for validating required and optional modules at startup.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReBACBrickManifest:
    """Brick manifest for the ReBAC module."""

    name: str = "rebac"
    protocol: str = "ReBACBrickProtocol"
    version: str = "1.0.0"
    config_schema: dict = field(
        default_factory=lambda: {
            "enforce_zone_isolation": {"type": "bool", "default": True},
            "enable_graph_limits": {"type": "bool", "default": True},
            "enable_leopard": {"type": "bool", "default": True},
            "enable_tiger_cache": {"type": "bool", "default": True},
        }
    )
    dependencies: list[str] = field(default_factory=list)


def verify_imports() -> dict[str, bool]:
    """Validate required and optional ReBAC imports at startup.

    Returns:
        Dict mapping module name to import success status.
    """
    results: dict[str, bool] = {}

    # Required modules
    for mod in [
        "nexus.rebac.manager",
        "nexus.rebac.types",
        "nexus.rebac.graph",
        "nexus.rebac.cache",
        "nexus.rebac.tuples",
    ]:
        try:
            importlib.import_module(mod)
            results[mod] = True
        except ImportError:
            results[mod] = False
            logger.error("Required ReBAC module missing: %s", mod)

    # Optional modules
    for mod in [
        "nexus.rebac.cache.tiger",
    ]:
        try:
            importlib.import_module(mod)
            results[mod] = True
        except ImportError:
            results[mod] = False
            logger.warning("Optional ReBAC module unavailable: %s", mod)

    return results
