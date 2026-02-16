"""Backward-compat re-export — moved to nexus.storage.persistent_view_postgres (#1524)."""

from nexus.storage.persistent_view_postgres import (
    PostgresPersistentViewStore as PostgresPersistentViewStore,
)

__all__ = ["PostgresPersistentViewStore"]
