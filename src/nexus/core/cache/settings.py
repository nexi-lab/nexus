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

    # L1 cache max size (entries)
    l1_cache_size: int = field(
        default_factory=lambda: int(os.environ.get("NEXUS_CACHE_L1_SIZE", "10000"))
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

        if self.dragonfly_pool_size <= 0:
            raise ValueError("NEXUS_DRAGONFLY_POOL_SIZE must be positive")

        if self.dragonfly_timeout <= 0:
            raise ValueError("NEXUS_DRAGONFLY_TIMEOUT must be positive")

        if self.dragonfly_connect_timeout <= 0:
            raise ValueError("NEXUS_DRAGONFLY_CONNECT_TIMEOUT must be positive")

        if self.dragonfly_pool_timeout <= 0:
            raise ValueError("NEXUS_DRAGONFLY_POOL_TIMEOUT must be positive")

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
            f"pool_size={self.dragonfly_pool_size}, "
            f"pool_timeout={self.dragonfly_pool_timeout}s, "
            f"keepalive={self.dragonfly_keepalive}, "
            f"l1_enabled={self.enable_l1_cache})"
        )
