"""Cache Coordinator - Backward compatibility shim.

Re-exports from nexus.rebac.cache.coordinator.
New code should import from nexus.rebac.cache.coordinator.
"""

from nexus.rebac.cache.coordinator import CacheCoordinator  # noqa: F401

__all__ = ["CacheCoordinator"]
