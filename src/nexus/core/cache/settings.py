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

    NEXUS_DRAGONFLY_POOL_SIZE: Connection pool size (default: 10)
    NEXUS_DRAGONFLY_TIMEOUT: Connection timeout in seconds (default: 5.0)
"""

import os
from dataclasses import dataclass, field
from typing import Literal, Optional


@dataclass
class CacheSettings:
    """Configuration for Nexus cache layer."""

    # Dragonfly connection (optional - if not set, use PostgreSQL)
    dragonfly_url: Optional[str] = field(
        default_factory=lambda: os.environ.get("NEXUS_DRAGONFLY_URL")
    )

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

    # Dragonfly connection pool size
    dragonfly_pool_size: int = field(
        default_factory=lambda: int(os.environ.get("NEXUS_DRAGONFLY_POOL_SIZE", "10"))
    )

    # Dragonfly connection timeout (seconds)
    dragonfly_timeout: float = field(
        default_factory=lambda: float(os.environ.get("NEXUS_DRAGONFLY_TIMEOUT", "5.0"))
    )

    # Enable L1 in-memory cache (optional layer before Dragonfly)
    enable_l1_cache: bool = field(
        default_factory=lambda: os.environ.get("NEXUS_CACHE_ENABLE_L1", "true").lower()
        == "true"
    )

    # L1 cache max size (entries)
    l1_cache_size: int = field(
        default_factory=lambda: int(os.environ.get("NEXUS_CACHE_L1_SIZE", "10000"))
    )

    def should_use_dragonfly(self) -> bool:
        """Determine if Dragonfly should be used based on config."""
        if self.cache_backend == "dragonfly":
            if not self.dragonfly_url:
                raise ValueError(
                    "NEXUS_CACHE_BACKEND=dragonfly but NEXUS_DRAGONFLY_URL not set"
                )
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
            raise ValueError(
                "NEXUS_CACHE_BACKEND=dragonfly requires NEXUS_DRAGONFLY_URL to be set"
            )

        if self.permission_ttl <= 0:
            raise ValueError("NEXUS_CACHE_PERMISSION_TTL must be positive")

        if self.permission_denial_ttl <= 0:
            raise ValueError("NEXUS_CACHE_DENIAL_TTL must be positive")

        if self.tiger_ttl <= 0:
            raise ValueError("NEXUS_CACHE_TIGER_TTL must be positive")

        if self.dragonfly_pool_size <= 0:
            raise ValueError("NEXUS_DRAGONFLY_POOL_SIZE must be positive")

        if self.dragonfly_timeout <= 0:
            raise ValueError("NEXUS_DRAGONFLY_TIMEOUT must be positive")

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
            f"pool_size={self.dragonfly_pool_size}, "
            f"l1_enabled={self.enable_l1_cache})"
        )
