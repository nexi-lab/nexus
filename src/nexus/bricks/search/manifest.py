"""Search brick manifest (Issue #1520).

Extends :class:`~nexus.contracts.brick_manifest.BrickManifest` with
search-specific configuration and module declarations.
"""

from dataclasses import dataclass, field

from nexus.contracts.brick_manifest import BrickManifest


@dataclass(frozen=True)
class SearchBrickManifest(BrickManifest):
    """Brick manifest for the search module."""

    name: str = "search"
    protocol: str = "SearchBrickProtocol"
    config_schema: dict[str, dict[str, object]] = field(
        default_factory=lambda: {
            "embedding_provider": {"type": "str", "default": "openai"},
            "search_mode": {"type": "str", "default": "hybrid"},
            "entropy_filtering": {"type": "bool", "default": False},
            "fusion_method": {"type": "str", "default": "rrf"},
            "chunk_size": {"type": "int", "default": 1500},
            "pool_min_size": {"type": "int", "default": 10},
            "pool_max_size": {"type": "int", "default": 50},
            "pool_recycle": {"type": "int", "default": 3600},
        }
    )
    required_modules: tuple[str, ...] = (
        "nexus.bricks.search.txtai_backend",
        "nexus.bricks.search.daemon",
        "nexus.bricks.search.indexing_service",
        "nexus.bricks.search.chunking",
        "nexus.bricks.search.config",
        "nexus.bricks.search.protocols",
        "nexus.bricks.search.result_builders",
        "nexus.bricks.search.search_service",
    )
    optional_modules: tuple[str, ...] = ("nexus.bricks.search.zoekt_client",)


def verify_imports() -> dict[str, bool]:
    """Convenience wrapper — instantiates manifest and verifies imports."""
    return SearchBrickManifest().verify_imports()
