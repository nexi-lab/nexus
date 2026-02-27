"""In-memory cache backend for dev/test environments.

Canonical implementation lives in ``nexus.contracts.cache_store`` (the pillar
module for the "Ephemeral" quartet member).  This module re-exports the class
so that existing ``from nexus.cache.inmemory import InMemoryCacheStore``
imports continue to work.

See contracts/cache_store.py for full documentation.
"""

from nexus.contracts.cache_store import InMemoryCacheStore

__all__ = ["InMemoryCacheStore"]
