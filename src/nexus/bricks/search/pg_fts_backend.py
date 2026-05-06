"""Postgres full-text search backend (Issue #3699).

Wraps the existing idx_chunks_bm25 pg_textsearch index (true k1+b BM25)
on document_chunks.chunk_text when the pg_search / pg_textsearch extension is
available. When the extension is absent, the backend falls back to PostgreSQL's
built-in ``to_tsvector`` / ``ts_rank_cd`` search so plain pgvector Postgres
deployments still return keyword and hybrid results.

Two BM25 modes:
  * keyword_search()         — chunk-level (one row per chunk)
  * keyword_search_pages()   — page-level (#3980; one row per path,
                                aggregating all chunks for that path)

Writes are NOT owned by this backend. ChunkStore.replace_document_chunks
already writes chunk_text + embedding atomically; pg_textsearch maintains
the BM25 index automatically. The add/upsert/delete methods on this
class are stubbed as NotImplementedError until T9 (daemon integration)
wires the call site through ChunkStore's actual API
(replace_document_chunks / delete_document_chunks). The stubs satisfy
the SearchBackend protocol shape so isinstance() checks pass immediately.

Page-BM25 approach (keyword_search_pages):
  The SQL uses a CTE to aggregate chunks per path into a page_text string,
  then applies pg_textsearch BM25 over page_text. This is the most accurate
  approach when pg_textsearch can process the CTE result. If the installed
  build cannot index CTE columns (operator-not-found / undefined-function
  raises ``ProgrammingError``), the backend transparently falls back to
  chunk-level BM25 with a wider ``k * 4`` and client-side page aggregation
  via :func:`result_builders._aggregate_chunks_to_pages`. The fallback
  decision is cached per-instance so we only try-and-catch once per
  daemon lifetime.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine

from nexus.bricks.search.result_builders import _aggregate_chunks_to_pages
from nexus.bricks.search.results import BaseSearchResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# pg_textsearch operator / score-function names.
# ---------------------------------------------------------------------------
# Verified against the Tiger Data / Timescale pg_textsearch convention as
# documented in docs/superpowers/specs/2026-05-03-drop-txtai-design.md:
#   @@@ is the BM25 match operator
#   paradedb.score(column) returns the BM25 relevance score for the row
#
# The migration (add_pg_textsearch_bm25_index.py) comment mentions <@> as an
# alternative Tiger Data operator; @@@/paradedb.score is the current canonical
# convention used by the paradedb-compatible build that pg_textsearch ships.
# If your installed build uses different names, change these two constants —
# nothing else in this file needs to change.
# ---------------------------------------------------------------------------
_BM25_MATCH_OP: str = "@@@"
_BM25_SCORE_FN: str = "paradedb.score"
_NATIVE_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_NATIVE_FTS_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "being",
    "by",
    "did",
    "do",
    "does",
    "for",
    "from",
    "had",
    "has",
    "have",
    "how",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "to",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
}


def _native_fts_query(query: str) -> str:
    """Build a broad native Postgres tsquery string for fallback search.

    ``plainto_tsquery`` combines normal text with AND, which is too strict for
    natural-language questions. A safe OR tsquery keeps fallback recall close
    to BM25 while PostgreSQL still handles stemming through ``to_tsquery``.
    """
    terms: list[str] = []
    seen: set[str] = set()
    for match in _NATIVE_FTS_TOKEN_RE.finditer(query.lower()):
        term = match.group(0)
        if len(term) < 2 or term in _NATIVE_FTS_STOPWORDS or term in seen:
            continue
        seen.add(term)
        terms.append(term)
    return " | ".join(terms)


def _native_like_patterns(query: str) -> dict[str, str]:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return {
        "phrase_pattern": f"%{escaped}%",
        "heading_pattern": f"#%: {escaped}%",
    }


def _is_missing_bm25_error(exc: BaseException) -> bool:
    """Return True for pg_search / pg_textsearch not-installed query failures."""
    orig = getattr(exc, "orig", None)
    message = str(orig if orig is not None else exc).lower()
    return (
        'schema "paradedb" does not exist' in message
        or "schema paradedb does not exist" in message
        or "function paradedb.score" in message
        or ("operator does not exist" in message and "@@@" in message)
        or 'access method "bm25" does not exist' in message
    )


class PgFtsBackend:
    """Postgres full-text-search backend using pg_textsearch BM25.

    Satisfies the SearchBackend protocol (T1). Reads use the existing
    idx_chunks_bm25 index on document_chunks.chunk_text. Writes are not
    owned here — see module docstring.

    Args:
        engine: Async SQLAlchemy engine pointed at the Nexus Postgres DB.
        chunk_store: Optional ChunkStore instance for write pass-through.
            Currently unused (write methods raise NotImplementedError until
            T9). Kept in the constructor so the daemon can wire it without
            changing the API.
    """

    def __init__(self, engine: AsyncEngine, chunk_store: Any | None = None) -> None:
        self._engine = engine
        self._chunk_store = chunk_store  # reserved for T9 write delegation
        # Cached page-BM25 strategy. ``None`` = untried (try CTE first),
        # ``True`` = CTE works, ``False`` = fall back to client-side
        # aggregation. Keeps us from try/except on every query once we
        # know which path the installed pg_textsearch build supports.
        self._page_cte_supported: bool | None = None
        # ``None`` = not checked yet, ``True`` = pg_search/pg_textsearch BM25
        # works, ``False`` = use built-in PostgreSQL FTS fallback.
        self._bm25_available: bool | None = None

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def startup(self) -> None:
        """Detect whether pg_search / pg_textsearch BM25 is available."""
        try:
            async with self._engine.connect() as conn:
                result = await conn.execute(
                    text("""
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_am
                        WHERE amname = 'bm25'
                    ) AS has_bm25
                    """)
                )
                self._bm25_available = bool(result.scalar())
        except Exception as exc:
            logger.debug("PgFtsBackend.startup: BM25 capability probe failed: %s", exc)
            self._bm25_available = None
        if self._bm25_available is False:
            self._page_cte_supported = False
        return None

    async def shutdown(self) -> None:
        """No-op: engine disposal is the caller's responsibility."""
        return None

    # -------------------------------------------------------------------------
    # Write pass-through stubs (T9 wires these to ChunkStore)
    # -------------------------------------------------------------------------
    # ChunkStore exposes replace_document_chunks(path_id, chunks) and
    # delete_document_chunks(path_id) — a different signature than the protocol's
    # add/upsert/delete(ids). The daemon (T9) will own the mapping. Until then,
    # these stubs keep the protocol satisfied at the isinstance() level.

    async def add(self, docs: Sequence[dict[str, Any]], *, zone_id: str) -> int:
        # TODO(T9): delegate to chunk_store.replace_document_chunks
        raise NotImplementedError(
            "PgFtsBackend.add: write path is owned by ChunkStore. "
            "Wire through daemon integration (T9)."
        )

    async def upsert(self, docs: Sequence[dict[str, Any]], *, zone_id: str) -> int:
        # TODO(T9): delegate to chunk_store.replace_document_chunks (idempotent)
        raise NotImplementedError(
            "PgFtsBackend.upsert: write path is owned by ChunkStore. "
            "Wire through daemon integration (T9)."
        )

    async def delete(self, ids: Sequence[str], *, zone_id: str) -> int:
        # TODO(T9): delegate to chunk_store.delete_document_chunks
        raise NotImplementedError(
            "PgFtsBackend.delete: write path is owned by ChunkStore. "
            "Wire through daemon integration (T9)."
        )

    # -------------------------------------------------------------------------
    # Keyword search — chunk-level
    # -------------------------------------------------------------------------

    async def keyword_search(
        self,
        query: str,
        path: str,
        k: int,
        zone_id: str,
    ) -> list[BaseSearchResult]:
        """BM25 chunk-level search.

        Returns up to *k* results ordered by BM25 score descending.
        All parameters are SQL-parameterized — no string interpolation.

        Args:
            query: BM25 search query string.
            path: Path prefix filter (e.g. "/zone/subdir/").
            k: Maximum number of results to return.
            zone_id: Zone isolation — only files in this zone are searched.

        Returns:
            List of BaseSearchResult ordered by score descending.
        """
        if self._bm25_available is False:
            return await self._keyword_search_native(query, path, k, zone_id)

        try:
            return await self._keyword_search_bm25(query, path, k, zone_id)
        except (ProgrammingError, DBAPIError) as exc:
            if not _is_missing_bm25_error(exc):
                raise
            logger.warning(
                "PgFtsBackend.keyword_search: BM25 query failed because "
                "pg_search/pg_textsearch is unavailable (%s: %s). Falling back "
                "to built-in PostgreSQL FTS for this process.",
                type(exc).__name__,
                exc,
            )
            self._bm25_available = False
            self._page_cte_supported = False
            return await self._keyword_search_native(query, path, k, zone_id)

    async def _keyword_search_bm25(
        self,
        query: str,
        path: str,
        k: int,
        zone_id: str,
    ) -> list[BaseSearchResult]:
        # NOTE: _BM25_MATCH_OP and _BM25_SCORE_FN are module-level string
        # constants, not user input, so the f-string here does NOT open an
        # SQL-injection surface. The runtime values (query, path, zone_id, k)
        # are all bound as :named parameters.
        sql = text(
            f"""
            SELECT c.chunk_id,
                   fp.virtual_path AS path,
                   c.chunk_text,
                   c.chunk_index,
                   {_BM25_SCORE_FN}(c.chunk_id) AS score
            FROM document_chunks c
            JOIN file_paths fp ON c.path_id = fp.path_id
            WHERE c.chunk_text {_BM25_MATCH_OP} :q
              AND fp.zone_id = :zone_id
              AND fp.virtual_path LIKE :prefix || '%'
              AND fp.deleted_at IS NULL
            ORDER BY score DESC
            LIMIT :k
            """
        )
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sql,
                        {"q": query, "prefix": path, "zone_id": zone_id, "k": k},
                    )
                )
                .mappings()
                .all()
            )

        return self._rows_to_results(rows, zone_id=zone_id)

    async def _keyword_search_native(
        self,
        query: str,
        path: str,
        k: int,
        zone_id: str,
    ) -> list[BaseSearchResult]:
        fts_query = _native_fts_query(query)
        if not fts_query:
            return []
        like_patterns = _native_like_patterns(query)

        sql = text("""
            WITH q AS (
              SELECT to_tsquery('english', :fts_query) AS query
            )
            SELECT c.chunk_id,
                   fp.virtual_path AS path,
                   c.chunk_text,
                   c.chunk_index,
                   (
                     ts_rank_cd(to_tsvector('english', c.chunk_text), q.query)
                     + CASE
                         WHEN c.chunk_text ILIKE :heading_pattern ESCAPE '\\' THEN 10.0
                         ELSE 0.0
                       END
                     + CASE
                         WHEN c.chunk_text ILIKE :phrase_pattern ESCAPE '\\' THEN 1.0
                         ELSE 0.0
                       END
                   ) AS score
            FROM document_chunks c
            JOIN file_paths fp ON c.path_id = fp.path_id
            CROSS JOIN q
            WHERE to_tsvector('english', c.chunk_text) @@ q.query
              AND fp.zone_id = :zone_id
              AND fp.virtual_path LIKE :prefix || '%'
              AND fp.deleted_at IS NULL
            ORDER BY score DESC
            LIMIT :k
        """)
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sql,
                        {
                            "fts_query": fts_query,
                            **like_patterns,
                            "prefix": path,
                            "zone_id": zone_id,
                            "k": k,
                        },
                    )
                )
                .mappings()
                .all()
            )

        return self._rows_to_results(rows, zone_id=zone_id)

    @staticmethod
    def _rows_to_results(rows: Sequence[Any], *, zone_id: str) -> list[BaseSearchResult]:
        return [
            BaseSearchResult(
                path=r["path"],
                chunk_text=r["chunk_text"],
                score=float(r["score"]),
                chunk_index=int(r["chunk_index"]),
                keyword_score=float(r["score"]),
                zone_id=zone_id,
            )
            for r in rows
        ]

    # -------------------------------------------------------------------------
    # Keyword search — page-level (#3980 parity)
    # -------------------------------------------------------------------------

    async def keyword_search_pages(
        self,
        query: str,
        path: str,
        k: int,
        zone_id: str,
    ) -> list[BaseSearchResult]:
        """BM25 page-level search (one result per path, not per chunk).

        Aggregates all chunks for each path into a single page_text string
        (ordered by chunk_index), then applies pg_textsearch BM25 over the
        page text. Returns at most *k* results ordered by BM25 score desc.

        This implements the #3980 parity requirement: a rare phrase buried
        deep in a multi-chunk document should surface the whole document
        rather than scoring only one chunk in isolation.

        Page-BM25 strategy: try CTE aggregation first. If the installed
        pg_textsearch build cannot apply ``@@@`` / ``paradedb.score`` over
        the CTE result (operator-not-found / function-not-found raises
        ``ProgrammingError``), fall back to chunk-level BM25 with a wider
        ``k * 4`` and client-side aggregation via
        ``_aggregate_chunks_to_pages``. The decision is cached on the
        instance so we only try-and-catch once per daemon lifetime.

        Args:
            query: BM25 search query string.
            path: Path prefix filter (e.g. "/zone/subdir/").
            k: Maximum number of page-level results to return.
            zone_id: Zone isolation — only files in this zone are searched.

        Returns:
            List of BaseSearchResult (one per path) ordered by score desc.
        """
        if self._bm25_available is False:
            return await self._keyword_search_pages_native(query, path, k, zone_id)

        if self._page_cte_supported is False:
            # Cached: previous attempt failed — go straight to fallback.
            return await self._keyword_search_pages_fallback(query, path, k, zone_id)

        try:
            results = await self._keyword_search_pages_cte(query, path, k, zone_id)
        except (ProgrammingError, DBAPIError) as exc:
            # ProgrammingError covers operator-not-found / function-not-found
            # in psycopg / asyncpg; DBAPIError is the broader catch for builds
            # that surface the same condition under a different SQLSTATE.
            # Anything else (timeouts, IntegrityError) we re-raise.
            if _is_missing_bm25_error(exc):
                logger.warning(
                    "PgFtsBackend.keyword_search_pages: BM25 query failed "
                    "because pg_search/pg_textsearch is unavailable (%s: %s). "
                    "Falling back to built-in PostgreSQL FTS for this process.",
                    type(exc).__name__,
                    exc,
                )
                self._bm25_available = False
                self._page_cte_supported = False
                return await self._keyword_search_pages_native(query, path, k, zone_id)

            if self._page_cte_supported is None:
                logger.warning(
                    "PgFtsBackend.keyword_search_pages: CTE-aggregated BM25 "
                    "failed on this pg_textsearch build (%s: %s). Falling "
                    "back to chunk-level BM25 + client-side page "
                    "aggregation; subsequent calls will skip the CTE attempt.",
                    type(exc).__name__,
                    exc,
                )
            self._page_cte_supported = False
            return await self._keyword_search_pages_fallback(query, path, k, zone_id)
        else:
            # Cache success on first hit so subsequent calls skip the
            # try/except overhead.
            if self._page_cte_supported is None:
                self._page_cte_supported = True
            return results

    async def _keyword_search_pages_cte(
        self,
        query: str,
        path: str,
        k: int,
        zone_id: str,
    ) -> list[BaseSearchResult]:
        sql = text(
            f"""
            WITH pages AS (
              SELECT fp.path_id,
                     fp.virtual_path,
                     fp.zone_id,
                     string_agg(c.chunk_text, ' ' ORDER BY c.chunk_index) AS page_text
              FROM document_chunks c
              JOIN file_paths fp ON c.path_id = fp.path_id
              WHERE fp.zone_id = :zone_id
                AND fp.virtual_path LIKE :prefix || '%'
                AND fp.deleted_at IS NULL
              GROUP BY fp.path_id, fp.virtual_path, fp.zone_id
            )
            SELECT path_id,
                   virtual_path AS path,
                   page_text,
                   {_BM25_SCORE_FN}(path_id) AS score
            FROM pages
            WHERE page_text {_BM25_MATCH_OP} :q
            ORDER BY score DESC
            LIMIT :k
            """
        )
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sql,
                        {"q": query, "prefix": path, "zone_id": zone_id, "k": k},
                    )
                )
                .mappings()
                .all()
            )

        return [
            BaseSearchResult(
                path=r["path"],
                chunk_text=r["page_text"],
                score=float(r["score"]),
                chunk_index=0,  # page-level result has no single chunk_index
                keyword_score=float(r["score"]),
                zone_id=zone_id,
            )
            for r in rows
        ]

    async def _keyword_search_pages_native(
        self,
        query: str,
        path: str,
        k: int,
        zone_id: str,
    ) -> list[BaseSearchResult]:
        fts_query = _native_fts_query(query)
        if not fts_query:
            return []
        like_patterns = _native_like_patterns(query)

        sql = text("""
            WITH q AS (
              SELECT to_tsquery('english', :fts_query) AS query
            ),
            pages AS (
              SELECT fp.path_id,
                     fp.virtual_path,
                     string_agg(c.chunk_text, ' ' ORDER BY c.chunk_index) AS page_text
              FROM document_chunks c
              JOIN file_paths fp ON c.path_id = fp.path_id
              WHERE fp.zone_id = :zone_id
                AND fp.virtual_path LIKE :prefix || '%'
                AND fp.deleted_at IS NULL
              GROUP BY fp.path_id, fp.virtual_path
            )
            SELECT path_id,
                   virtual_path AS path,
                   page_text,
                   (
                     ts_rank_cd(to_tsvector('english', page_text), q.query)
                     + CASE
                         WHEN page_text ILIKE :heading_pattern ESCAPE '\\' THEN 10.0
                         ELSE 0.0
                       END
                     + CASE
                         WHEN page_text ILIKE :phrase_pattern ESCAPE '\\' THEN 1.0
                         ELSE 0.0
                       END
                   ) AS score
            FROM pages
            CROSS JOIN q
            WHERE to_tsvector('english', page_text) @@ q.query
            ORDER BY score DESC
            LIMIT :k
        """)
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sql,
                        {
                            "fts_query": fts_query,
                            **like_patterns,
                            "prefix": path,
                            "zone_id": zone_id,
                            "k": k,
                        },
                    )
                )
                .mappings()
                .all()
            )

        return [
            BaseSearchResult(
                path=r["path"],
                chunk_text=r["page_text"],
                score=float(r["score"]),
                chunk_index=0,
                keyword_score=float(r["score"]),
                zone_id=zone_id,
            )
            for r in rows
        ]

    async def _keyword_search_pages_fallback(
        self,
        query: str,
        path: str,
        k: int,
        zone_id: str,
    ) -> list[BaseSearchResult]:
        """Client-side page aggregation when pg_textsearch can't index CTEs.

        Pulls the top ``k * 4`` chunk-level matches and aggregates them into
        pages via ``_aggregate_chunks_to_pages``. The widened ``k * 4`` keeps
        recall close to the CTE path: a rare phrase that lands in chunk 30 of
        a long page still shows up because we collected enough siblings to
        max-pool at the page level.
        """
        chunks = await self.keyword_search(query, path, k * 4, zone_id)
        if not chunks:
            return []

        # ``_aggregate_chunks_to_pages`` works on dict rows; convert from
        # BaseSearchResult and back so we can reuse the shared helper.
        rows = [
            {
                "path": r.path,
                "chunk_text": r.chunk_text,
                "score": r.score,
                "chunk_index": r.chunk_index,
                "keyword_score": r.keyword_score,
            }
            for r in chunks
        ]
        # chunks_per_page=1 collapses to one row per page (page-level shape).
        aggregated = _aggregate_chunks_to_pages(rows, chunks_per_page=1)[:k]
        return [
            BaseSearchResult(
                path=r["path"],
                chunk_text=r["chunk_text"],
                score=float(r["score"]),
                chunk_index=int(r.get("chunk_index", 0) or 0),
                keyword_score=float(r.get("keyword_score", r["score"]) or 0.0),
                zone_id=zone_id,
            )
            for r in aggregated
        ]

    # -------------------------------------------------------------------------
    # Semantic search — no-op (lives in PgVectorBackend, T6)
    # -------------------------------------------------------------------------

    async def semantic_search(
        self,
        query_vector: Sequence[float],  # noqa: ARG002
        path: str,  # noqa: ARG002
        k: int,  # noqa: ARG002
        zone_id: str,  # noqa: ARG002
    ) -> list[BaseSearchResult]:
        """Not implemented in this backend — semantic search lives in PgVectorBackend (T6)."""
        return []
