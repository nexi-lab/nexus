"""Auth brick manifest and startup validation (Issue #1399).

Declares the Auth brick's metadata and provides verify_imports()
for validating required and optional modules at startup.
"""

import importlib
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class AuthBrickManifest:
    """Brick manifest for the Auth module."""

    name: str = "auth"
    protocol: str = "AuthBrickProtocol"
    version: str = "1.0.0"
    config_schema: dict = field(
        default_factory=lambda: {
            "auth_type": {
                "type": "str",
                "enum": ["static", "database", "local", "oidc", "multi-oidc"],
            },
            "cache_ttl_seconds": {"type": "int", "default": 900},
            "cache_max_size": {"type": "int", "default": 1000},
        }
    )
    dependencies: list[str] = field(default_factory=list)

def verify_imports() -> dict[str, bool]:
    """Validate required and optional Auth imports at startup.

    Returns:
        Dict mapping module name to import success status.
    """
    results: dict[str, bool] = {}

    # Required modules
    for mod in [
        "nexus.auth.types",
        "nexus.auth.protocol",
        "nexus.auth.constants",
        "nexus.auth.cache",
        "nexus.auth.providers.base",
        "nexus.auth.providers.discriminator",
    ]:
        try:
            importlib.import_module(mod)
            results[mod] = True
        except ImportError:
            results[mod] = False
            logger.error("Required Auth module missing: %s", mod)

    # Optional modules
    for mod in [
        "nexus.auth.providers.oidc",
        "nexus.auth.providers.database_key",
        "nexus.auth.service",
    ]:
        try:
            importlib.import_module(mod)
            results[mod] = True
        except ImportError:
            results[mod] = False
            logger.warning("Optional Auth module unavailable: %s", mod)

    return results
