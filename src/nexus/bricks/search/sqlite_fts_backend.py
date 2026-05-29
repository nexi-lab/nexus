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
import logging
import sqlite3
import time
from collections.abc import Sequence
from typing import Any

from nexus.bricks.search.pg_fts_backend import _first_corpus_token
from nexus.bricks.search.results import BaseSearchResult

logger = logging.getLogger(__name__)

# Index-preload warm token (Issue #4269). A neutral, non-stopword content word
# used only as a fallback when no real corpus token can be sampled (empty
# corpus or sampling error); a real sampled token is preferred so the warm
# touches actual postings/heap (Codex R5).
_PRELOAD_WARM_TOKEN = "data"

# Backend-level deadline for the preload warm (Issue #4269 Codex R9). The
# daemon wraps preload() in asyncio.wait_for, but cancelling that await only
# abandons the Future — it cannot stop a synchronous sqlite3 query running in
# the to_thread worker. A progress handler that aborts past this deadline gives
# a REAL cancellation so a hung warm releases its connection instead of
# contending with the first real queries. Mirrors the daemon's 30s wait_for.
_PRELOAD_WARM_TIMEOUT_SECONDS = 30.0
# Progress-handler granularity: callback every N VM instructions. Small enough
# to abort promptly past the deadline, large enough to keep per-query overhead
# negligible.
_PRELOAD_PROGRESS_INSTRUCTIONS = 10000


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
        *,
        timing: dict[str, float] | None = None,
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
            timing: Optional dict to accumulate phase timings into. When
                provided, the wall-clock of the FTS5 MATCH query is added to
                ``timing['index_load_ms']`` (Issue #4269).

        Returns:
            List of BaseSearchResult ordered best-first (highest score first).
        """

        def _search() -> tuple[list[BaseSearchResult], float]:
            with self._connect() as conn:
                index_load_start = time.perf_counter()
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
                exec_ms = (time.perf_counter() - index_load_start) * 1000
            results = [
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
            return results, exec_ms

        results, exec_ms = await asyncio.to_thread(_search)
        if timing is not None:
            timing["index_load_ms"] = timing.get("index_load_ms", 0.0) + exec_ms
        return results

    async def preload(self) -> float:
        """Fault the FTS5 keyword hot-path pages into RAM (Issue #4269).

        Issues a single bounded query that mirrors ``keyword_search`` — the
        ``document_chunks_fts MATCH`` inverted-index lookup, the
        ``document_chunks`` + ``file_paths`` joins, and ``bm25()`` ordering —
        so the kernel reads the SAME FTS5 index and ranking pages the first
        real query will touch, not just a plain rowid scan. Returns elapsed ms
        on success.

        NOT internally fail-soft (Codex R4): a genuine warm failure (e.g. the
        FTS5 vtable not yet created) is raised so the daemon records a failed
        preload instead of reporting a misleading successful warm. The daemon's
        ``_preload_search_index`` is the fail-soft boundary.

        The warm token is sampled from real indexed chunks (Codex R5) so the
        MATCH query hits actual postings + ranked rows rather than being a
        zero-hit lookup. If the corpus is non-empty but no indexable token can
        be sampled, the warm cannot be proven effective and ``_warm`` raises
        (Codex R6) so the daemon records a failed preload. Falls back to a fixed
        token only when the corpus is empty or sampling errors.
        """

        def _warm() -> None:
            with self._connect() as conn:
                # Backend-level deadline: abort the query if it runs past the
                # timeout so a hung warm actually stops and releases the
                # connection, rather than orphaning a thread (Codex R9).
                deadline = time.monotonic() + _PRELOAD_WARM_TIMEOUT_SECONDS

                def _abort_if_past_deadline() -> int:
                    return 1 if time.monotonic() > deadline else 0

                conn.set_progress_handler(_abort_if_past_deadline, _PRELOAD_PROGRESS_INSTRUCTIONS)
                try:
                    self._run_warm_queries(conn)
                finally:
                    conn.set_progress_handler(None, 0)

        start = time.perf_counter()
        await asyncio.to_thread(_warm)
        return (time.perf_counter() - start) * 1000

    def _run_warm_queries(self, conn: sqlite3.Connection) -> None:
        """Sample a real corpus token and run the ranked MATCH warm (sync, runs
        inside the to_thread worker under a progress-handler deadline)."""
        token = _PRELOAD_WARM_TOKEN
        token_is_real = False
        # Sampling errors are NOT swallowed (Codex R10): a failure must not be
        # misread as "empty corpus" and let a zero-hit fixed-token warm report
        # success. Let it propagate so the daemon records a failed preload.
        rows = conn.execute(
            "SELECT dc.chunk_text "
            "FROM document_chunks dc "
            "JOIN file_paths fp ON dc.path_id = fp.path_id "
            "WHERE fp.deleted_at IS NULL AND dc.chunk_text IS NOT NULL "
            "LIMIT 20"
        ).fetchall()
        corpus_nonempty = bool(rows)
        for row in rows:
            if row[0]:
                tok = _first_corpus_token(str(row[0]))
                if tok:
                    token = tok
                    token_is_real = True
                    break
        if corpus_nonempty and not token_is_real:
            raise RuntimeError(
                "SqliteFtsBackend.preload: no indexable token found in "
                "sampled corpus; warm would be a zero-hit lookup"
            )
        warm_rows = conn.execute(
            "SELECT dc.chunk_id "
            "FROM document_chunks_fts "
            "JOIN document_chunks dc ON dc.rowid = document_chunks_fts.rowid "
            "JOIN file_paths fp ON dc.path_id = fp.path_id "
            "WHERE document_chunks_fts MATCH ? "
            "  AND fp.deleted_at IS NULL "
            "ORDER BY bm25(document_chunks_fts) "
            "LIMIT 64",
            [token],
        ).fetchall()
        # A token sampled from a live chunk must match a fresh FTS5 index; zero
        # matches means stale/mismatched index → not effectively warmed, so fail
        # rather than report success (R7).
        if token_is_real and not warm_rows:
            raise RuntimeError(
                "SqliteFtsBackend.preload: warm MATCH returned no rows for "
                "a sampled corpus token; FTS5 index stale or mismatched"
            )

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
