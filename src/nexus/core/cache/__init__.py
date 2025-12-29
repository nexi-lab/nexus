"""Nexus Cache Layer - Pluggable caching backends for permissions and metadata.

This module provides a unified caching interface that supports multiple backends:
- Dragonfly (Redis-compatible, recommended for production)
- PostgreSQL (fallback, uses existing rebac_check_cache table)

Configuration:
    Set NEXUS_DRAGONFLY_URL to enable Dragonfly backend:

    NEXUS_DRAGONFLY_URL=redis://localhost:6379

    If not set, falls back to PostgreSQL-based caching.

Usage:
    from nexus.core.cache import get_permission_cache, get_tiger_cache

    cache = get_permission_cache()
    result = await cache.get(subject_type, subject_id, permission, ...)
"""

from nexus.core.cache.base import PermissionCacheProtocol, TigerCacheProtocol
from nexus.core.cache.factory import CacheFactory
from nexus.core.cache.settings import CacheSettings

__all__ = [
    "CacheSettings",
    "CacheFactory",
    "PermissionCacheProtocol",
    "TigerCacheProtocol",
]
