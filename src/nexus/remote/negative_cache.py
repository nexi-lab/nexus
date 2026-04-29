"""Negative cache abstraction for remote clients.

Provides a protocol-based negative cache (tracks known-absent paths) to
replace the ad-hoc Bloom filter embedded in BaseRemoteNexusFS.

Follows the same pattern as CacheStoreABC + NullCacheStore: a protocol
defines the interface, concrete implementations provide the behavior,
and a NullNegativeCache gives graceful degradation when no implementation
is available.

The Bloom filter remains the default implementation — it's the right data
structure for probabilistic set-membership testing on the client side.
The protocol simply makes the dependency explicit and injectable.
"""

import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class NegativeCache(Protocol):
    """Protocol for negative caches that track known-absent keys.

    A negative cache remembers keys that are known to NOT exist, enabling
    callers to skip expensive lookups (e.g., RPCs) for absent resources.

    Implementations:
    - BloomNegativeCache: Probabilistic (Bloom filter), space-efficient
    - NullNegativeCache: No-op, always returns False (cache miss)
    """

    def check(self, key: str) -> bool:
        """Check if key is known to be absent.

        Returns True if the key is (probably) absent, False if unknown.
        May return false positives (Bloom filter), never false negatives.
        """
        ...

    def add(self, key: str) -> None:
        """Record that a key is known to be absent."""
        ...

    def clear(self) -> None:
        """Clear all entries."""
        ...


class BloomNegativeCache:
    """Bloom filter-based negative cache using nexus_runtime.BloomFilter.

    Space-efficient probabilistic set membership testing. False positives
    are possible (a path may appear absent when it exists), but false
    negatives never occur (if check() returns False, the path is
    genuinely unknown).
    """

    def __init__(self, bloom_filter: Any) -> None:
        self._bloom = bloom_filter

    def check(self, key: str) -> bool:
        return bool(self._bloom.might_exist(key))

    def add(self, key: str) -> None:
        self._bloom.add(key)

    def clear(self) -> None:
        self._bloom.clear()

    @property
    def memory_bytes(self) -> int:
        """Memory usage of the underlying Bloom filter."""
        return int(self._bloom.memory_bytes)


class NullNegativeCache:
    """No-op negative cache — always reports cache miss.

    Used when the Bloom filter backend (nexus_runtime) is not available.
    All lookups miss, so callers always proceed to the server RPC.
    """

    def check(self, _key: str) -> bool:
        return False

    def add(self, _key: str) -> None:
        pass

    def clear(self) -> None:
        pass


def create_negative_cache(
    capacity: int = 100_000,
    fp_rate: float = 0.01,
) -> NegativeCache:
    """Create a NegativeCache backed by nexus_runtime.BloomFilter.

    Args:
        capacity: Maximum number of entries the Bloom filter can hold.
        fp_rate: Target false-positive rate (0.01 = 1%).

    Returns:
        A BloomNegativeCache instance.
    """
    # RUST_FALLBACK: BloomFilter (optional — returns None if stale/absent binary)
    from nexus._rust_compat import BloomFilter

    if BloomFilter is None:
        logger.debug(
            "BloomFilter unavailable (stale or absent nexus_runtime) — using no-op negative cache"
        )
        return NullNegativeCache()

    bloom = BloomFilter(capacity, fp_rate)
    cache = BloomNegativeCache(bloom)
    logger.debug(
        "Negative cache initialized: capacity=%d, fp_rate=%s, memory=%d bytes",
        capacity,
        fp_rate,
        cache.memory_bytes,
    )
    return cache
