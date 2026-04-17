"""Path context descriptions (Issue #3773).

Stores admin-configured, zone-scoped mappings from path prefix to human-readable
description. Used by the search daemon to attach a ``context`` field to each
search result via longest-prefix match. In-memory cache is in this module too.
"""

from __future__ import annotations

import asyncio
import builtins
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from nexus.contracts.constants import ROOT_ZONE_ID

# Default LRU cap on the number of zones PathContextCache retains. A
# multi-tenant deployment with many short-lived zones could otherwise
# accumulate per-zone locks + record lists forever (Issue #3773 review).
_DEFAULT_MAX_ZONES = 2048


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
        now = datetime.now(UTC).replace(tzinfo=None)
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
                # SQLite 3.24+ supports ON CONFLICT DO UPDATE with the same
                # semantics as Postgres — preserves `id` and `created_at`,
                # bumps `updated_at`. Avoids the `INSERT OR REPLACE` approach
                # which would churn `id` and break any future FK to this row.
                await session.execute(
                    text(
                        """
                        INSERT INTO path_contexts
                            (zone_id, path_prefix, description, created_at, updated_at)
                        VALUES
                            (:zone_id, :path_prefix, :description, :now, :now)
                        ON CONFLICT (zone_id, path_prefix) DO UPDATE
                        SET description = excluded.description,
                            updated_at  = excluded.updated_at
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

    async def zone_fingerprint(self, zone_id: str) -> tuple[int, datetime | None]:
        """Return ``(row_count, max_updated_at)`` for a zone.

        Issue #3773 review: ``max_updated_at`` alone misses row deletions —
        deleting a row whose ``updated_at`` is below the zone's max leaves
        the max unchanged, so a cache keyed on that value alone keeps
        serving the deleted entry. Including ``COUNT(*)`` makes the
        freshness token detect row removals as well.
        """
        async with self._async_session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT COUNT(*), MAX(updated_at) FROM path_contexts "
                        "WHERE zone_id = :zone_id"
                    ),
                    {"zone_id": zone_id},
                )
            ).one()
        count = int(row[0] or 0)
        stamp = _coerce_datetime(row[1]) if row[1] is not None else None
        return (count, stamp)

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


class PathContextCache:
    """In-memory cache of path contexts keyed by zone, with longest-prefix lookup.

    - Per-zone ``asyncio.Lock`` serializes refreshes.
    - Each lookup cheaply checks ``store.max_updated_at(zone_id)`` and reloads
      when the cached stamp is stale.
    - Records are kept sorted by ``len(path_prefix)`` DESC so the first
      slash-boundary match is the longest prefix.
    """

    def __init__(
        self,
        *,
        store: PathContextStore,
        max_zones: int = _DEFAULT_MAX_ZONES,
    ) -> None:
        self._store = store
        self._max_zones = max_zones
        # Freshness token is ``(row_count, max_updated_at)`` — including count
        # catches deletes that leave the zone's max unchanged (Issue #3773
        # review feedback). OrderedDict enables LRU eviction on insertion so
        # the cache can't grow without bound across many short-lived zones.
        self._entries: OrderedDict[
            str,
            tuple[tuple[int, datetime | None], builtins.list[PathContextRecord]],
        ] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, zone_id: str) -> asyncio.Lock:
        # ``setdefault(zone_id, asyncio.Lock())`` constructs a Lock on every
        # call whether or not it ends up used — wasted allocation on hot
        # search paths. Use get-or-create instead.
        lock = self._locks.get(zone_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[zone_id] = lock
        return lock

    async def refresh_if_stale(self, zone_id: str) -> None:
        db_fp = await self._store.zone_fingerprint(zone_id)
        cached = self._entries.get(zone_id)
        if cached is not None and cached[0] == db_fp:
            return
        async with self._lock_for(zone_id):
            # Re-check after lock acquisition — another task may have refreshed.
            db_fp = await self._store.zone_fingerprint(zone_id)
            cached = self._entries.get(zone_id)
            if cached is not None and cached[0] == db_fp:
                return
            records = await self._store.load_all_for_zone(zone_id)
            records.sort(key=lambda r: len(r.path_prefix), reverse=True)
            self._entries[zone_id] = (db_fp, records)
            self._entries.move_to_end(zone_id)
            # LRU-bound: evict the oldest zones when we exceed the cap. The
            # zone we just inserted is at the newest end and safe. Other
            # zones' locks can be dropped along with their entries — any
            # concurrent refresh task for an evicted zone still holds its
            # own lock reference and will just finish and rebuild.
            while len(self._entries) > self._max_zones:
                evicted_zone, _ = self._entries.popitem(last=False)
                self._locks.pop(evicted_zone, None)

    def lookup_cached(self, zone_id: str | None, path: str) -> str | None:
        """Pure in-memory longest-prefix lookup. Assumes the caller has already
        awaited ``refresh_if_stale(effective_zone)`` when freshness matters.

        Returns the longest-matching description for ``path`` in ``zone_id``,
        or None when no prefix matches or the zone has no cached entries.
        """
        effective_zone = zone_id or ROOT_ZONE_ID
        cached = self._entries.get(effective_zone)
        if cached is None:
            return None
        _, records = cached
        for record in records:
            prefix = record.path_prefix
            if prefix == "":
                return record.description
            if path == prefix or path.startswith(prefix + "/"):
                return record.description
        return None

    async def lookup(self, zone_id: str | None, path: str) -> str | None:
        """Async convenience: refresh then read. Prefer ``refresh_if_stale`` +
        ``lookup_cached`` when looking up many paths in the same zone.
        """
        effective_zone = zone_id or ROOT_ZONE_ID
        await self.refresh_if_stale(effective_zone)
        return self.lookup_cached(zone_id, path)
