"""Iterator caching layer - Backward compatibility shim.

Re-exports from nexus.rebac.cache.iterator.
New code should import from nexus.rebac.cache.iterator.
"""

from nexus.rebac.cache.iterator import (  # noqa: F401
    CachedResult,
    CursorExpiredError,
    IteratorCache,
)

__all__ = ["CachedResult", "CursorExpiredError", "IteratorCache"]
