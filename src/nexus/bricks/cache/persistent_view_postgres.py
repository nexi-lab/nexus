"""Backward-compatibility shim — moved to nexus.storage.persistent_view_postgres.

.. deprecated:: Issue #2055
    Import from ``nexus.storage.persistent_view_postgres`` instead.
    PostgresPersistentViewStore has RecordStore affinity, not cache affinity.
"""

from nexus.storage.persistent_view_postgres import PostgresPersistentViewStore

__all__ = ["PostgresPersistentViewStore"]
