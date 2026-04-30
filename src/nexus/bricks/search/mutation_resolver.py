"""Shared mutation resolution for search consumers.

Normalizes search mutation events into resolved document payloads and provides
small in-process caching so multiple consumers can reuse the same file read
and path lookup within a worker cycle.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text as sa_text
from sqlalchemy.exc import ProgrammingError

from nexus.bricks.search.mutation_events import SearchMutationEvent
from nexus.contracts.constants import ROOT_ZONE_ID


@dataclass(frozen=True)
class ResolvedMutation:
    """Resolved mutation payload shared across search consumers."""

    event: SearchMutationEvent
    zone_id: str
    virtual_path: str
    path_id: str
    doc_id: str
    content: str | None = None
    path_id_resolved: bool = True


class MutationResolver:
    """Resolve search mutation events into indexable payloads."""

    def __init__(
        self,
        *,
        file_reader: Any | None,
        async_session_factory: Any | None,
        cache_ttl_seconds: float = 30.0,
        lookup_batch_size: int = 250,
    ) -> None:
        self._file_reader = file_reader
        self._async_session_factory = async_session_factory
        self._cache_ttl_seconds = cache_ttl_seconds
        self._lookup_batch_size = max(1, int(lookup_batch_size))
        self._cache: dict[str, tuple[float, ResolvedMutation]] = {}

    @staticmethod
    def _path_key(zone_id: str, virtual_path: str) -> tuple[str, str]:
        return (zone_id, virtual_path)

    def set_file_reader(self, file_reader: Any | None) -> None:
        self._file_reader = file_reader

    def invalidate_path(self, path: str) -> None:
        keys_to_delete = [
            key
            for key, (_, resolved) in self._cache.items()
            if resolved.event.path == path or resolved.virtual_path == path
        ]
        for key in keys_to_delete:
            self._cache.pop(key, None)

    async def resolve_batch(self, events: list[SearchMutationEvent]) -> list[ResolvedMutation]:
        """Resolve a batch of events with shared DB lookups and cache reuse."""
        if not events:
            return []

        now = time.monotonic()
        resolved: list[ResolvedMutation | None] = [None] * len(events)
        unresolved_indices: list[int] = []
        lookup_candidates: list[tuple[str, str, str, int]] = []

        for idx, event in enumerate(events):
            cached = self._cache.get(event.event_id)
            if cached is not None and (now - cached[0]) < self._cache_ttl_seconds:
                resolved[idx] = cached[1]
                continue
            unresolved_indices.append(idx)
            lookup_candidates.extend(self._lookup_candidates(event))

        path_id_map = await self._lookup_path_ids(lookup_candidates)
        content_map = await self._lookup_content(events, unresolved_indices)

        for idx in unresolved_indices:
            event = events[idx]
            virtual_path = event.virtual_path
            resolved_path_id = path_id_map.get(self._path_key(event.zone_id, virtual_path))
            path_id_resolved = resolved_path_id is not None
            path_id = resolved_path_id if resolved_path_id is not None else virtual_path
            zone_id = event.zone_id
            doc_id = f"{zone_id}:{virtual_path}" if zone_id != ROOT_ZONE_ID else virtual_path
            mutation = ResolvedMutation(
                event=event,
                zone_id=zone_id,
                virtual_path=virtual_path,
                path_id=path_id,
                doc_id=doc_id,
                content=content_map.get(event.event_id),
                path_id_resolved=path_id_resolved,
            )
            self._cache[event.event_id] = (now, mutation)
            resolved[idx] = mutation

        return [item for item in resolved if item is not None]

    async def _lookup_path_ids(
        self,
        lookup_candidates: list[tuple[str, str, str, int]],
    ) -> dict[tuple[str, str], str]:
        if not lookup_candidates or self._async_session_factory is None:
            return {}

        path_id_map: dict[tuple[str, str], str] = {}
        unique_candidates = list(dict.fromkeys(lookup_candidates))
        async with self._async_session_factory() as session:
            for offset in range(0, len(unique_candidates), self._lookup_batch_size):
                batch = unique_candidates[offset : offset + self._lookup_batch_size]
                values_sql, params = self._build_lookup_values(batch)
                result = await session.execute(
                    sa_text(
                        f"""
                        WITH lookup(
                            zone_id,
                            virtual_path,
                            canonical_virtual_path,
                            lookup_rank
                        ) AS (
                            VALUES {values_sql}
                        )
                        SELECT l.zone_id, l.canonical_virtual_path, fp.path_id
                        FROM file_paths fp
                        JOIN lookup l
                          ON fp.zone_id = l.zone_id
                         AND fp.virtual_path = l.virtual_path
                        WHERE fp.deleted_at IS NULL
                        ORDER BY l.lookup_rank
                        """
                    ),
                    params,
                )
                for row in result.fetchall():
                    key = self._path_key(str(row[0]), str(row[1]))
                    path_id_map.setdefault(key, str(row[2]))
        return path_id_map

    async def _lookup_content(
        self,
        events: list[SearchMutationEvent],
        unresolved_indices: list[int],
    ) -> dict[str, str]:
        content_map: dict[str, str] = {}
        update_events = [
            events[idx] for idx in unresolved_indices if events[idx].op.value == "upsert"
        ]
        if not update_events:
            return content_map

        missing_events: list[SearchMutationEvent] = []
        for event in update_events:
            content = await self._read_content(event.path, event.virtual_path)
            if content:
                content_map[event.event_id] = content
            else:
                missing_events.append(event)

        if missing_events and self._async_session_factory is not None:
            lookup_candidates: list[tuple[str, str, str, int]] = []
            for event in missing_events:
                lookup_candidates.extend(self._lookup_candidates(event))
            db_content = await self._lookup_content_cache(lookup_candidates)
            for event in missing_events:
                content = db_content.get(self._path_key(event.zone_id, event.virtual_path))
                if content:
                    content_map[event.event_id] = content

        return content_map

    async def _read_content(self, scoped_path: str, virtual_path: str) -> str | None:
        if self._file_reader is None:
            return None

        try:
            scoped_content = await self._file_reader.read_text(scoped_path)
            return scoped_content if isinstance(scoped_content, str) else None
        except Exception:
            with contextlib.suppress(OSError, ValueError, Exception):
                virtual_content = await self._file_reader.read_text(virtual_path)
                return virtual_content if isinstance(virtual_content, str) else None
        return None

    async def _lookup_content_cache(
        self,
        lookup_candidates: list[tuple[str, str, str, int]],
    ) -> dict[tuple[str, str], str]:
        if not lookup_candidates or self._async_session_factory is None:
            return {}

        content_map: dict[tuple[str, str], str] = {}
        unique_candidates = list(dict.fromkeys(lookup_candidates))
        async with self._async_session_factory() as session:
            try:
                for offset in range(0, len(unique_candidates), self._lookup_batch_size):
                    batch = unique_candidates[offset : offset + self._lookup_batch_size]
                    values_sql, params = self._build_lookup_values(batch)
                    result = await session.execute(
                        sa_text(
                            f"""
                            WITH lookup(
                                zone_id,
                                virtual_path,
                                canonical_virtual_path,
                                lookup_rank
                            ) AS (
                                VALUES {values_sql}
                            )
                            SELECT l.zone_id, l.canonical_virtual_path, cc.content_text
                            FROM lookup l
                            JOIN file_paths fp
                              ON fp.zone_id = l.zone_id
                             AND fp.virtual_path = l.virtual_path
                            JOIN content_cache cc
                              ON cc.path_id = fp.path_id
                            WHERE fp.deleted_at IS NULL
                              AND cc.content_text IS NOT NULL
                            ORDER BY l.lookup_rank
                            """
                        ),
                        params,
                    )
                    for row in result.fetchall():
                        if row[2]:
                            content_map.setdefault(
                                self._path_key(str(row[0]), str(row[1])),
                                str(row[2]),
                            )
            except ProgrammingError as exc:
                if self._is_missing_table_error(exc):
                    return {}
                raise
        return content_map

    @staticmethod
    def _lookup_candidates(event: SearchMutationEvent) -> list[tuple[str, str, str, int]]:
        return MutationResolver._build_path_lookup_candidates(
            event.zone_id,
            event.path,
            event.virtual_path,
        )

    @staticmethod
    def _build_path_lookup_candidates(
        zone_id: str,
        scoped_path: str,
        virtual_path: str,
    ) -> list[tuple[str, str, str, int]]:
        canonical = virtual_path
        candidates: list[str] = []

        def add(candidate: str) -> None:
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        add(canonical)
        add(scoped_path)
        if canonical.startswith("/"):
            add(f"/zone/{zone_id}{canonical}")

        return [(zone_id, candidate, canonical, rank) for rank, candidate in enumerate(candidates)]

    def _build_lookup_values(
        self,
        lookup_candidates: list[tuple[str, str, str, int]],
    ) -> tuple[str, dict[str, str | int]]:
        params: dict[str, str | int] = {}
        values_parts: list[str] = []
        for idx, (zone_id, virtual_path, canonical_virtual_path, lookup_rank) in enumerate(
            lookup_candidates
        ):
            params[f"zone_id_{idx}"] = zone_id
            params[f"virtual_path_{idx}"] = virtual_path
            params[f"canonical_virtual_path_{idx}"] = canonical_virtual_path
            params[f"lookup_rank_{idx}"] = lookup_rank
            values_parts.append(
                f"(:zone_id_{idx}, :virtual_path_{idx}, "
                f":canonical_virtual_path_{idx}, :lookup_rank_{idx})"
            )
        return ", ".join(values_parts), params

    @staticmethod
    def _is_missing_table_error(exc: ProgrammingError) -> bool:
        message = str(getattr(exc, "orig", exc)).lower()
        return "content_cache" in message and (
            "does not exist" in message or "no such table" in message or "undefinedtable" in message
        )
