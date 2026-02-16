"""Backward-compat re-export — moved to nexus.backends.caching_wrapper (#1524)."""

from nexus.backends.caching_wrapper import (
    CacheStrategy as CacheStrategy,
    CacheWrapperConfig as CacheWrapperConfig,
    CachingBackendWrapper as CachingBackendWrapper,
)

__all__ = ["CacheStrategy", "CacheWrapperConfig", "CachingBackendWrapper"]
