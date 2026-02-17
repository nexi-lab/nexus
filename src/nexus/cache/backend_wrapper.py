"""Backward-compatibility shim — moved to nexus.backends.caching_wrapper.

Import from nexus.backends.caching_wrapper instead.
"""

from nexus.backends.caching_wrapper import (
    CacheStrategy,
    CacheWrapperConfig,
    CachingBackendWrapper,
)

__all__ = [
    "CacheStrategy",
    "CacheWrapperConfig",
    "CachingBackendWrapper",
]
