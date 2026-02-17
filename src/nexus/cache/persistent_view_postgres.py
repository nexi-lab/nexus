"""Backward-compatibility shim — moved to nexus.storage.persistent_view_postgres.

Import from nexus.storage.persistent_view_postgres instead.
"""

from nexus.storage.persistent_view_postgres import PostgresPersistentViewStore

__all__ = ["PostgresPersistentViewStore"]
