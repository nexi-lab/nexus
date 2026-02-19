"""Backwards-compatible re-export — canonical location is nexus.backends.caching_wrapper.

CachingBackendWrapper is a Backend decorator (same-Protocol recursive wrapper).
It belongs in backends/ alongside DelegatingBackend and LoggingBackendWrapper.

New code should import directly::

    from nexus.backends.caching_wrapper import CachingBackendWrapper, CacheWrapperConfig
"""

from nexus.backends.caching_wrapper import (
    CacheStrategy,
    CacheWrapperConfig,
    CachingBackendWrapper,
)

__all__ = ["CacheStrategy", "CacheWrapperConfig", "CachingBackendWrapper"]
