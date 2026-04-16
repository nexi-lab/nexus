"""Path context descriptions (Issue #3773).

Stores admin-configured, zone-scoped mappings from path prefix to human-readable
description. Used by the search daemon to attach a ``context`` field to each
search result via longest-prefix match. In-memory cache is in this module too.
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from nexus.contracts.constants import ROOT_ZONE_ID  # noqa: F401  (re-exported for callers)


@dataclass(frozen=True)
class PathContextRecord:
    """One row in the path_contexts table."""

    zone_id: str
    path_prefix: str
    description: str
    created_at: datetime
    updated_at: datetime


class PathContextStore:
    """Async CRUD for the path_contexts table.

    Follows the raw-SQL pattern used by ChunkStore (src/nexus/bricks/search/chunk_store.py).
    """

    def __init__(self, *, async_session_factory: Any, db_type: str = "sqlite") -> None:
        self._async_session_factory = async_session_factory
        self._db_type = db_type

    async def upsert(self, zone_id: str, path_prefix: str, description: str) -> None:
        """Insert or replace a context row. updated_at refreshed on replace."""
        now = datetime.utcnow()
        async with self._async_session_factory() as session:
            if self._db_type == "postgresql":
                await session.execute(
                    text(
                        """
                        INSERT INTO path_contexts
                            (zone_id, path_prefix, description, created_at, updated_at)
                        VALUES
                            (:zone_id, :path_prefix, :description, :now, :now)
                        ON CONFLICT (zone_id, path_prefix) DO UPDATE
                        SET description = EXCLUDED.description,
                            updated_at  = EXCLUDED.updated_at
                        """
                    ),
                    {
                        "zone_id": zone_id,
                        "path_prefix": path_prefix,
                        "description": description,
                        "now": now,
                    },
                )
            else:
                # SQLite: preserve created_at on replace via COALESCE lookup.
                await session.execute(
                    text(
                        """
                        INSERT OR REPLACE INTO path_contexts
                            (zone_id, path_prefix, description, created_at, updated_at)
                        VALUES
                            (:zone_id, :path_prefix, :description,
                             COALESCE(
                                (SELECT created_at FROM path_contexts
                                 WHERE zone_id = :zone_id AND path_prefix = :path_prefix),
                                :now),
                             :now)
                        """
                    ),
                    {
                        "zone_id": zone_id,
                        "path_prefix": path_prefix,
                        "description": description,
                        "now": now,
                    },
                )
            await session.commit()

    async def delete(self, zone_id: str, path_prefix: str) -> bool:
        """Delete one row. Returns True if a row was removed."""
        async with self._async_session_factory() as session:
            result = await session.execute(
                text(
                    "DELETE FROM path_contexts "
                    "WHERE zone_id = :zone_id AND path_prefix = :path_prefix"
                ),
                {"zone_id": zone_id, "path_prefix": path_prefix},
            )
            await session.commit()
            return (result.rowcount or 0) > 0

    async def list(self, zone_id: str | None = None) -> builtins.list[PathContextRecord]:
        """List contexts. When zone_id is None, returns rows for all zones."""
        query = (
            "SELECT zone_id, path_prefix, description, created_at, updated_at FROM path_contexts"
        )
        params: dict[str, Any] = {}
        if zone_id is not None:
            query += " WHERE zone_id = :zone_id"
            params["zone_id"] = zone_id
        query += " ORDER BY zone_id, path_prefix"
        async with self._async_session_factory() as session:
            rows = (await session.execute(text(query), params)).all()
        return [
            PathContextRecord(
                zone_id=row[0],
                path_prefix=row[1],
                description=row[2],
                created_at=_coerce_datetime(row[3]),
                updated_at=_coerce_datetime(row[4]),
            )
            for row in rows
        ]

    async def max_updated_at(self, zone_id: str) -> datetime | None:
        """Return the max updated_at for a zone, or None if empty."""
        async with self._async_session_factory() as session:
            row = (
                await session.execute(
                    text("SELECT MAX(updated_at) FROM path_contexts WHERE zone_id = :zone_id"),
                    {"zone_id": zone_id},
                )
            ).scalar()
        return _coerce_datetime(row) if row is not None else None

    async def load_all_for_zone(self, zone_id: str) -> builtins.list[PathContextRecord]:
        """Load every context row for one zone."""
        return await self.list(zone_id=zone_id)


def _coerce_datetime(value: Any) -> datetime:
    """SQLite + aiosqlite can return datetimes as ISO strings; normalize to datetime."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        # SQLite stores "YYYY-MM-DD HH:MM:SS[.ffffff]" — fromisoformat handles both.
        return datetime.fromisoformat(value)
    raise TypeError(f"Unexpected datetime-like value from DB: {value!r}")


# Issue #3773: PathContextCache is defined in Task 7. This placeholder keeps
# imports stable so tests for the cache can be added in Task 7 without
# re-editing import sites.
class PathContextCache:  # Placeholder — full implementation in Task 7.
    def __init__(self, *, store: PathContextStore) -> None:
        self._store = store

    async def refresh_if_stale(self, zone_id: str) -> None:
        raise NotImplementedError("PathContextCache is wired up in Task 7.")

    async def lookup(self, zone_id: str | None, path: str) -> str | None:
        raise NotImplementedError("PathContextCache is wired up in Task 7.")
