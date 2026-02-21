"""Caching protocol contracts (Issue #1628, #2362).

Two distinct caching contracts:

- ``CacheConfigContract`` — the 3-attribute mixin contract that CacheService
  relies on (session_factory, zone_id, l1_only). Formerly ``CachingConnectorContract``.

- ``CachingConnectorContract`` — wrapping-chain cache methods for
  CachingBackendWrapper (get_cache_stats, clear_cache, describe).

References:
    - Issue #1628: Split CacheConnectorMixin into focused units
    - Issue #2362: ConnectorProtocol wrapping chains
    - docs/design/cache-layer.md
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CacheConfigContract(Protocol):
    """Contract for connectors that support caching via CacheService.

    Documents the attributes that CacheService expects from its connector
    reference. Connectors inheriting CacheConnectorMixin implicitly satisfy
    this contract.

    Attributes:
        session_factory: SQLAlchemy session factory (None for L1-only mode)
        zone_id: Cache zone identifier (defaults to "root")
        l1_only: If True, skip L2 (disk/DB) caching entirely
    """

    session_factory: Any | None
    zone_id: str | None
    l1_only: bool


@runtime_checkable
class CachingConnectorContract(Protocol):
    """Wrapping-chain cache capability protocol.

    Satisfied by CachingBackendWrapper and any other wrapper that provides
    cache introspection and management at the connector level.

    Methods:
        get_cache_stats: Return cache hit/miss/error counters.
        clear_cache: Clear all cached entries and reset stats.
        describe: Return the wrapper chain description string.
    """

    def get_cache_stats(self) -> dict[str, Any]: ...

    def clear_cache(self) -> None: ...

    def describe(self) -> str: ...
