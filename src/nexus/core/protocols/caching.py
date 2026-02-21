"""Caching connector contract protocol (Issue #1628).

Documents the implicit contract that CacheService relies on when
interacting with connectors. The 14+ getattr() calls in cache_mixin.py
previously relied on duck typing; this protocol makes the contract explicit.

References:
    - Issue #1628: Split CacheConnectorMixin into focused units
    - docs/design/cache-layer.md
"""

from typing import Any, Protocol, runtime_checkable

@runtime_checkable
class CachingConnectorContract(Protocol):
    """Contract for connectors that support caching.

    Documents the attributes and methods that CacheService expects
    from its connector reference. Connectors inheriting CacheConnectorMixin
    implicitly satisfy this contract.

    Attributes:
        session_factory: SQLAlchemy session factory (None for L1-only mode)
        zone_id: Cache zone identifier (defaults to "root")
        l1_only: If True, skip L2 (disk/DB) caching entirely
    """

    session_factory: Any | None
    zone_id: str | None
    l1_only: bool
