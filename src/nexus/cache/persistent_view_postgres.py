"""Backwards-compatible re-export — canonical location is nexus.storage.persistent_view_postgres.

PostgresPersistentViewStore is a persistence concern (RecordStore-backed),
not a cache concern. It belongs in storage/ alongside other PostgreSQL stores.

New code should import directly::

    from nexus.storage.persistent_view_postgres import PostgresPersistentViewStore
"""

from nexus.storage.persistent_view_postgres import PostgresPersistentViewStore

__all__ = ["PostgresPersistentViewStore"]
