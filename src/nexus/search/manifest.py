"""Search brick manifest and startup validation (Issue #1520).

Declares the search brick's metadata and provides verify_imports()
for validating required and optional modules at startup.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchBrickManifest:
    """Brick manifest for the search module."""

    name: str = "search"
    protocol: str = "SearchBrickProtocol"
    version: str = "1.0.0"
    config_schema: dict = field(
        default_factory=lambda: {
            "embedding_provider": {"type": "str", "default": "openai"},
            "search_mode": {"type": "str", "default": "hybrid"},
            "entropy_filtering": {"type": "bool", "default": False},
            "fusion_method": {"type": "str", "default": "rrf"},
        }
    )
    dependencies: list[str] = field(default_factory=list)


def verify_imports() -> dict[str, bool]:
    """Validate required and optional search imports at startup.

    Returns:
        Dict mapping module name to import success status.
    """
    results: dict[str, bool] = {}

    # Required modules
    for mod in [
        "nexus.search.semantic",
        "nexus.search.async_search",
        "nexus.search.fusion",
        "nexus.search.chunking",
        "nexus.search.embeddings",
        "nexus.search.vector_db",
    ]:
        try:
            importlib.import_module(mod)
            results[mod] = True
        except ImportError:
            results[mod] = False
            logger.error("Required search module missing: %s", mod)

    # Optional modules
    for mod in [
        "nexus.search.bm25s_search",
        "nexus.search.zoekt_client",
        "nexus.search.graph_store",
        "nexus.core.trigram_fast",
    ]:
        try:
            importlib.import_module(mod)
            results[mod] = True
        except ImportError:
            results[mod] = False
            logger.warning("Optional search module unavailable: %s", mod)

    return results
