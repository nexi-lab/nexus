"""Cache configuration settings.

Environment variables:
    NEXUS_DRAGONFLY_URL: Redis-compatible URL for Dragonfly
        Example: redis://localhost:6379
        If not set, PostgreSQL-based caching is used

    NEXUS_CACHE_BACKEND: Cache backend selection
        - "auto": Use Dragonfly if URL set, else PostgreSQL (default)
        - "dragonfly": Force Dragonfly (fails if URL not set)
        - "postgres": Force PostgreSQL (ignores Dragonfly URL)

    NEXUS_CACHE_PERMISSION_TTL: TTL for permission grants (default: 300s)
    NEXUS_CACHE_DENIAL_TTL: TTL for permission denials (default: 60s)
    NEXUS_CACHE_TIGER_TTL: TTL for Tiger cache entries (default: 3600s)
    NEXUS_CACHE_EMBEDDING_TTL: TTL for embedding cache entries (default: 86400s / 24h)

    # Tiered TTL by Relation Type (Issue #1077)
    NEXUS_CACHE_TTL_OWNER: TTL for owner permissions (default: 3600s / 1 hour)
    NEXUS_CACHE_TTL_EDITOR: TTL for editor permissions (default: 600s / 10 min)
    NEXUS_CACHE_TTL_VIEWER: TTL for viewer permissions (default: 600s / 10 min)
    NEXUS_CACHE_TTL_INHERITED: TTL for inherited permissions (default: 300s / 5 min)

    # Invalidation Strategy (Issue #1077)
    NEXUS_CACHE_INVALIDATION_MODE: Invalidation strategy
        - "targeted": Path-specific invalidation using indexes (default)
        - "tenant_wide": Legacy tenant-wide invalidation

    # Connection Pool Settings (Issue #1075)
    NEXUS_DRAGONFLY_POOL_SIZE: Max connections in pool (default: 50)
    NEXUS_DRAGONFLY_TIMEOUT: Socket timeout in seconds (default: 30.0)
    NEXUS_DRAGONFLY_CONNECT_TIMEOUT: Connection timeout in seconds (default: 5.0)
    NEXUS_DRAGONFLY_POOL_TIMEOUT: Wait time for available connection (default: 20.0)
    NEXUS_DRAGONFLY_KEEPALIVE: Enable TCP keepalive (default: true)
    NEXUS_DRAGONFLY_RETRY_ON_TIMEOUT: Retry on timeout errors (default: true)
"""

import os
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class CacheSettings:
    """Configuration for Nexus cache layer."""

    # Dragonfly connection (optional - if not set, use PostgreSQL)
    dragonfly_url: str | None = field(default_factory=lambda: os.environ.get("NEXUS_DRAGONFLY_URL"))

    # Backend selection: auto, dragonfly, postgres
    cache_backend: Literal["auto", "dragonfly", "postgres"] = field(
        default_factory=lambda: os.environ.get("NEXUS_CACHE_BACKEND", "auto")  # type: ignore
    )

    # Permission cache TTL (seconds)
    permission_ttl: int = field(
        default_factory=lambda: int(os.environ.get("NEXUS_CACHE_PERMISSION_TTL", "300"))
    )

    # Denial cache TTL - shorter for security (seconds)
    permission_denial_ttl: int = field(
        default_factory=lambda: int(os.environ.get("NEXUS_CACHE_DENIAL_TTL", "60"))
    )

    # Tiger cache TTL (seconds)
    tiger_ttl: int = field(
        default_factory=lambda: int(os.environ.get("NEXUS_CACHE_TIGER_TTL", "3600"))
    )

    # Embedding cache TTL (seconds) - Issue #950
    embedding_ttl: int = field(
        default_factory=lambda: int(os.environ.get("NEXUS_CACHE_EMBEDDING_TTL", "86400"))
    )

    # Tiered TTL by relation type (Issue #1077)
    # Owner permissions rarely change, so use longer TTL
    ttl_owner: int = field(
        default_factory=lambda: int(os.environ.get("NEXUS_CACHE_TTL_OWNER", "3600"))
    )

    # Editor permissions change more often
    ttl_editor: int = field(
        default_factory=lambda: int(os.environ.get("NEXUS_CACHE_TTL_EDITOR", "600"))
    )

    # Viewer permissions - similar to editor
    ttl_viewer: int = field(
        default_factory=lambda: int(os.environ.get("NEXUS_CACHE_TTL_VIEWER", "600"))
    )

    # Inherited permissions - shorter TTL since they depend on parent changes
    ttl_inherited: int = field(
        default_factory=lambda: int(os.environ.get("NEXUS_CACHE_TTL_INHERITED", "300"))
    )

    # Invalidation strategy (Issue #1077)
    # "targeted": Use path indexes for O(1) invalidation
    # "tenant_wide": Legacy behavior - scan all keys
    invalidation_mode: Literal["targeted", "tenant_wide"] = field(
        default_factory=lambda: os.environ.get("NEXUS_CACHE_INVALIDATION_MODE", "targeted")  # type: ignore
    )

    # Dragonfly connection pool size (Issue #1075: increased from 10 to 50)
    dragonfly_pool_size: int = field(
        default_factory=lambda: int(os.environ.get("NEXUS_DRAGONFLY_POOL_SIZE", "50"))
    )

    # Dragonfly socket timeout in seconds (Issue #1075: increased from 5 to 30)
    dragonfly_timeout: float = field(
        default_factory=lambda: float(os.environ.get("NEXUS_DRAGONFLY_TIMEOUT", "30.0"))
    )

    # Dragonfly connection timeout in seconds (Issue #1075)
    dragonfly_connect_timeout: float = field(
        default_factory=lambda: float(os.environ.get("NEXUS_DRAGONFLY_CONNECT_TIMEOUT", "5.0"))
    )

    # Dragonfly pool timeout - wait time for available connection (Issue #1075)
    dragonfly_pool_timeout: float = field(
        default_factory=lambda: float(os.environ.get("NEXUS_DRAGONFLY_POOL_TIMEOUT", "20.0"))
    )

    # Enable TCP keepalive for cloud/NAT environments (Issue #1075)
    dragonfly_keepalive: bool = field(
        default_factory=lambda: os.environ.get("NEXUS_DRAGONFLY_KEEPALIVE", "true").lower()
        == "true"
    )

    # Retry on timeout errors (Issue #1075)
    dragonfly_retry_on_timeout: bool = field(
        default_factory=lambda: os.environ.get("NEXUS_DRAGONFLY_RETRY_ON_TIMEOUT", "true").lower()
        == "true"
    )

    # Enable L1 in-memory cache (optional layer before Dragonfly)
    enable_l1_cache: bool = field(
        default_factory=lambda: os.environ.get("NEXUS_CACHE_ENABLE_L1", "true").lower() == "true"
    )

    # L1 cache max size (entries) - Issue #1077: increased from 10K to 50K
    l1_cache_size: int = field(
        default_factory=lambda: int(os.environ.get("NEXUS_CACHE_L1_SIZE", "50000"))
    )

    def should_use_dragonfly(self) -> bool:
        """Determine if Dragonfly should be used based on config."""
        if self.cache_backend == "dragonfly":
            if not self.dragonfly_url:
                raise ValueError("NEXUS_CACHE_BACKEND=dragonfly but NEXUS_DRAGONFLY_URL not set")
            return True
        if self.cache_backend == "postgres":
            return False
        # "auto" mode - use Dragonfly if URL is set
        return self.dragonfly_url is not None

    def validate(self) -> None:
        """Validate configuration."""
        if self.cache_backend not in ("auto", "dragonfly", "postgres"):
            raise ValueError(
                f"Invalid NEXUS_CACHE_BACKEND: {self.cache_backend}. "
                "Must be 'auto', 'dragonfly', or 'postgres'"
            )

        if self.cache_backend == "dragonfly" and not self.dragonfly_url:
            raise ValueError("NEXUS_CACHE_BACKEND=dragonfly requires NEXUS_DRAGONFLY_URL to be set")

        if self.permission_ttl <= 0:
            raise ValueError("NEXUS_CACHE_PERMISSION_TTL must be positive")

        if self.permission_denial_ttl <= 0:
            raise ValueError("NEXUS_CACHE_DENIAL_TTL must be positive")

        if self.tiger_ttl <= 0:
            raise ValueError("NEXUS_CACHE_TIGER_TTL must be positive")

        if self.embedding_ttl <= 0:
            raise ValueError("NEXUS_CACHE_EMBEDDING_TTL must be positive")

        # Tiered TTL validation (Issue #1077)
        if self.ttl_owner <= 0:
            raise ValueError("NEXUS_CACHE_TTL_OWNER must be positive")
        if self.ttl_editor <= 0:
            raise ValueError("NEXUS_CACHE_TTL_EDITOR must be positive")
        if self.ttl_viewer <= 0:
            raise ValueError("NEXUS_CACHE_TTL_VIEWER must be positive")
        if self.ttl_inherited <= 0:
            raise ValueError("NEXUS_CACHE_TTL_INHERITED must be positive")

        # Invalidation mode validation (Issue #1077)
        if self.invalidation_mode not in ("targeted", "tenant_wide"):
            raise ValueError(
                f"Invalid NEXUS_CACHE_INVALIDATION_MODE: {self.invalidation_mode}. "
                "Must be 'targeted' or 'tenant_wide'"
            )

        if self.dragonfly_pool_size <= 0:
            raise ValueError("NEXUS_DRAGONFLY_POOL_SIZE must be positive")

        if self.dragonfly_timeout <= 0:
            raise ValueError("NEXUS_DRAGONFLY_TIMEOUT must be positive")

        if self.dragonfly_connect_timeout <= 0:
            raise ValueError("NEXUS_DRAGONFLY_CONNECT_TIMEOUT must be positive")

        if self.dragonfly_pool_timeout <= 0:
            raise ValueError("NEXUS_DRAGONFLY_POOL_TIMEOUT must be positive")

    def get_ttl_for_relation(self, relation: str, is_inherited: bool = False) -> int:
        """Get TTL for a specific relation type (Issue #1077).

        Args:
            relation: The relation type (e.g., "owner", "editor", "viewer", "read", "write")
            is_inherited: Whether this is an inherited permission (shorter TTL)

        Returns:
            TTL in seconds based on relation stability
        """
        if is_inherited:
            return self.ttl_inherited

        # Map relations to their TTL
        relation_lower = relation.lower()

        # Owner-level relations (most stable, longest TTL)
        if relation_lower in ("owner", "direct_owner", "admin"):
            return self.ttl_owner

        # Editor-level relations
        if relation_lower in ("editor", "write", "contributor", "can_write"):
            return self.ttl_editor

        # Viewer-level relations
        if relation_lower in ("viewer", "read", "can_read", "reader"):
            return self.ttl_viewer

        # Default to permission_ttl for unknown relations
        return self.permission_ttl

    @classmethod
    def from_env(cls) -> "CacheSettings":
        """Create settings from environment variables."""
        settings = cls()
        settings.validate()
        return settings

    def __repr__(self) -> str:
        # Hide URL password if present
        url_display = self.dragonfly_url
        if url_display and "@" in url_display:
            # redis://:password@host:port -> redis://***@host:port
            parts = url_display.split("@")
            url_display = f"{parts[0].split(':')[0]}:***@{parts[1]}"

        return (
            f"CacheSettings("
            f"backend={self.cache_backend}, "
            f"dragonfly_url={url_display}, "
            f"permission_ttl={self.permission_ttl}s, "
            f"denial_ttl={self.permission_denial_ttl}s, "
            f"tiger_ttl={self.tiger_ttl}s, "
            f"embedding_ttl={self.embedding_ttl}s, "
            f"tiered_ttl={{owner={self.ttl_owner}s, editor={self.ttl_editor}s, "
            f"viewer={self.ttl_viewer}s, inherited={self.ttl_inherited}s}}, "
            f"invalidation_mode={self.invalidation_mode}, "
            f"pool_size={self.dragonfly_pool_size}, "
            f"pool_timeout={self.dragonfly_pool_timeout}s, "
            f"keepalive={self.dragonfly_keepalive}, "
            f"l1_enabled={self.enable_l1_cache}, "
            f"l1_size={self.l1_cache_size})"
        )
