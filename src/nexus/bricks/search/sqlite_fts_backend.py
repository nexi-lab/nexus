"""SQLite FTS5 backend (Issue #3699).

Replaces bm25s_search.py for the SANDBOX profile. Uses FTS5 native bm25()
ranking — no in-memory index, no rebuild on query. JOINs file_paths for
zone + path filtering (mirrors PgFtsBackend's shape).

sqlite3 is sync; calls are wrapped in asyncio.to_thread to keep the event
loop responsive (matches sqlite_vec_backend.py pattern from #3778).

Writes are NOT owned here — chunk_store.replace_document_chunks writes
chunk_text, and the FTS5 triggers from the T4 migration keep
document_chunks_fts in sync. add/upsert/delete raise NotImplementedError
until T9 wires the call site through ChunkStore's actual API.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from typing import Any

from nexus.bricks.search.results import BaseSearchResult


class SqliteFtsBackend:
    """SQLite FTS5 keyword search backend for the SANDBOX profile.

    Uses the ``document_chunks_fts`` FTS5 virtual table + the ``file_paths``
    JOIN for zone isolation and path-prefix filtering. The FTS5 vtable is
    kept in sync by triggers created in the T4 migration.

    Satisfies the SearchBackend protocol (T1).  Writes are not owned here
    — see module docstring and PgFtsBackend for the same pattern.

    Args:
        db_path: Absolute path to the SQLite database file.
        chunk_store: Reserved for T9 write delegation. Currently unused;
            write methods raise NotImplementedError until T9 wires the
            call site through ChunkStore's actual API.
    """

    def __init__(self, db_path: str, chunk_store: Any | None = None) -> None:
        self._db_path = db_path
        self._chunk_store = chunk_store  # reserved for T9 write delegation

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """No-op: FTS5 vtable is maintained by SQLite triggers automatically."""
        return None

    async def shutdown(self) -> None:
        """No-op: connections are opened and closed per-query."""
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open a new sqlite3 connection with Row factory for named access."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Write pass-through stubs (T9 wires these to ChunkStore)
    # ------------------------------------------------------------------
    # ChunkStore exposes replace_document_chunks(path_id, chunks) and
    # delete_document_chunks(path_id) — a different signature than the
    # protocol's add/upsert/delete(ids). The daemon (T9) will own the
    # mapping. Until then, these stubs keep the protocol satisfied at
    # the isinstance() level (same pattern as PgFtsBackend T5 + PgVectorBackend T6).

    async def add(self, docs: Sequence[dict[str, Any]], *, zone_id: str) -> int:
        # TODO(T9): delegate to chunk_store.replace_document_chunks
        raise NotImplementedError(
            "SqliteFtsBackend.add: write path is owned by ChunkStore. "
            "Wire through daemon integration (T9)."
        )

    async def upsert(self, docs: Sequence[dict[str, Any]], *, zone_id: str) -> int:
        # TODO(T9): delegate to chunk_store.replace_document_chunks (idempotent)
        raise NotImplementedError(
            "SqliteFtsBackend.upsert: write path is owned by ChunkStore. "
            "Wire through daemon integration (T9)."
        )

    async def delete(self, ids: Sequence[str], *, zone_id: str) -> int:
        # TODO(T9): delegate to chunk_store.delete_document_chunks
        raise NotImplementedError(
            "SqliteFtsBackend.delete: write path is owned by ChunkStore. "
            "Wire through daemon integration (T9)."
        )

    # ------------------------------------------------------------------
    # Keyword search — chunk-level
    # ------------------------------------------------------------------

    async def keyword_search(
        self,
        query: str,
        path: str,
        k: int,
        zone_id: str,
    ) -> list[BaseSearchResult]:
        """FTS5 BM25 chunk-level keyword search.

        Runs the FTS5 MATCH query against ``document_chunks_fts``, JOINs
        ``document_chunks`` and ``file_paths`` for zone isolation + path
        prefix filtering, and orders by ``bm25()`` ascending (lower is
        better in FTS5). The score is sign-flipped on output so that
        ``BaseSearchResult.score`` follows the "higher = more relevant"
        convention used across all search backends.

        Args:
            query: FTS5 query string (plain keywords, phrases, or operators).
            path: Virtual-path prefix to restrict results (e.g. ``"/z/"``).
            k: Maximum number of results to return.
            zone_id: Zone identifier for row-level isolation.

        Returns:
            List of BaseSearchResult ordered best-first (highest score first).
        """

        def _search() -> list[BaseSearchResult]:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT dc.chunk_id, fp.virtual_path AS path, dc.chunk_text, "
                    "       dc.chunk_index, "
                    "       bm25(document_chunks_fts) AS score "
                    "FROM document_chunks_fts "
                    "JOIN document_chunks dc ON dc.rowid = document_chunks_fts.rowid "
                    "JOIN file_paths fp ON dc.path_id = fp.path_id "
                    "WHERE document_chunks_fts MATCH ? "
                    "  AND fp.zone_id = ? "
                    "  AND fp.virtual_path LIKE ? || '%' "
                    "  AND fp.deleted_at IS NULL "
                    "ORDER BY score "  # bm25() returns negative; ASC = best first
                    "LIMIT ?",
                    [query, zone_id, path, k],
                ).fetchall()
            return [
                BaseSearchResult(
                    path=r["path"],
                    chunk_text=r["chunk_text"],
                    score=-float(r["score"]),  # flip sign so higher = better
                    chunk_index=int(r["chunk_index"]),
                    keyword_score=-float(r["score"]),
                    zone_id=zone_id,
                )
                for r in rows
            ]

        return await asyncio.to_thread(_search)

    # ------------------------------------------------------------------
    # Semantic search — delegated to SqliteVecBackend
    # ------------------------------------------------------------------

    async def semantic_search(
        self,
        query_vector: Sequence[float],  # noqa: ARG002
        path: str,  # noqa: ARG002
        k: int,  # noqa: ARG002
        zone_id: str,  # noqa: ARG002
    ) -> list[BaseSearchResult]:
        """No-op: semantic search lives in SqliteVecBackend."""
        return []
