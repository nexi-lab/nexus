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
  The backend pulls an over-fetched chunk-level candidate set through the
  indexed BM25/native-FTS query, then max-pools those candidates to one row
  per path. This keeps page search on the same indexed hot path as chunk
  search. Avoid building a page aggregation CTE before matching: that forces
  PostgreSQL to aggregate/rank the corpus for a single-hit query.
"""

from __future__ import annotations

import logging
import re
import time
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
# Index-preload warm token (Issue #4269). Must NOT be an English stopword: a
# stopword reduces to an empty ``to_tsquery`` in the native-FTS fallback, which
# matches nothing and faults in no index pages — defeating the warm. A neutral
# content word issues a real index lookup on both the BM25 and native paths
# regardless of whether the corpus actually contains it.
_PRELOAD_WARM_TOKEN: str = "data"
_PAGE_SEARCH_CANDIDATE_MULTIPLIER = 8
_PAGE_SEARCH_MIN_CANDIDATES = 64
# Unicode-aware token pattern (Issue #4269 Codex R9): ``[^\W_]+`` matches word
# characters EXCLUDING underscore, with Unicode semantics, so accented Latin and
# other non-ASCII terms that PostgreSQL FTS and SQLite FTS5 (unicode61) index are
# not falsely treated as untokenizable by the warm-token sampler.
_NATIVE_FTS_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
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


def _page_candidate_limit(k: int) -> int:
    if k <= 0:
        return 0
    return max(k * _PAGE_SEARCH_CANDIDATE_MULTIPLIER, _PAGE_SEARCH_MIN_CANDIDATES)


def _first_corpus_token(text_value: str) -> str | None:
    """Pick a warm token from chunk text, matching the query analyzers' token
    acceptance (Issue #4269 Codex R5/R8).

    Minimum length 2 — the same threshold ``_native_fts_query`` uses — so a
    searchable acronym/short-token corpus (e.g. ``"AI ML UX"``) is not falsely
    rejected. A non-stopword token is preferred (more likely to survive
    Postgres stopword removal), but we fall back to ANY >=2-char token rather
    than declaring "no token": SQLite FTS5 (porter/unicode61, no stopword list)
    matches stopwords too, and the caller's matched-row check is the final
    per-backend effectiveness guarantee. Returns None only when the text has no
    >=2-char alphanumeric token at all.
    """
    tokens = [
        match.group(0)
        for match in _NATIVE_FTS_TOKEN_RE.finditer(text_value.lower())
        if len(match.group(0)) >= 2
    ]
    for tok in tokens:
        if tok not in _NATIVE_FTS_STOPWORDS:
            return tok
    return tokens[0] if tokens else None


def _record_index_load(timing: dict[str, float] | None, start: float) -> None:
    """Accumulate the index-touching DB-query wall-clock into index_load_ms.

    Issue #4269: ``+=`` so repeated keyword queries within one search (e.g.
    chunk + page legs) sum into a single index_load_ms figure rather than
    clobbering each other.
    """
    if timing is None:
        return
    elapsed_ms = (time.perf_counter() - start) * 1000
    timing["index_load_ms"] = timing.get("index_load_ms", 0.0) + elapsed_ms


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
        return None

    async def shutdown(self) -> None:
        """No-op: engine disposal is the caller's responsibility."""
        return None

    def _bm25_warm_query(self) -> Any:
        """Ranked BM25 warm query mirroring ``_keyword_search_bm25`` (Codex R2):
        selects paradedb.score and ORDER BY it so the planner executes the
        scoring/ranking path, plus the file_paths JOIN + deleted_at filter."""
        return text(
            f"SELECT c.chunk_id, {_BM25_SCORE_FN}(c.chunk_id) AS score "
            f"FROM document_chunks c "
            f"JOIN file_paths fp ON c.path_id = fp.path_id "
            f"WHERE c.chunk_text {_BM25_MATCH_OP} :q "
            f"AND fp.deleted_at IS NULL "
            f"ORDER BY score DESC "
            f"LIMIT 64"
        )

    def _native_warm_query(self) -> Any:
        """Ranked native-FTS warm query mirroring ``_keyword_search_native``."""
        return text(
            "SELECT c.chunk_id, "
            "       ts_rank_cd(to_tsvector('english', c.chunk_text), q.query) AS score "
            "FROM document_chunks c "
            "JOIN file_paths fp ON c.path_id = fp.path_id "
            "CROSS JOIN (SELECT to_tsquery('english', :q) AS query) q "
            "WHERE to_tsvector('english', c.chunk_text) @@ q.query "
            "AND fp.deleted_at IS NULL "
            "ORDER BY score DESC "
            "LIMIT 64"
        )

    async def preload(self) -> float:
        """Fault the keyword-search hot-path pages into RAM (Issue #4269).

        Issues a single bounded, zone-agnostic warm query that mirrors the real
        keyword query's table/index access pattern — the ranked BM25 (or
        tsvector) index on ``document_chunks.chunk_text``, the ``file_paths``
        JOIN, and the ``deleted_at`` filter — so the kernel faults all of those
        pages off the (possibly network-attached) volume before the first real
        query. Returns elapsed ms on success.

        NOT internally fail-soft (Codex R4): a genuine warm failure is raised so
        the daemon records it as a failed preload rather than reporting a
        misleading "successful" warm over a still-cold index. The daemon's
        ``_preload_search_index`` provides the fail-soft boundary (it never lets
        a preload error abort startup). If BM25 is detected unavailable, the
        native-FTS fallback — the path the first real query will actually use —
        is warmed before returning, instead of leaving it cold.

        The warm token is sampled from real indexed chunks (Codex R5) so the
        query matches actual rows and faults the term postings + joined/ranked
        heap pages, not just the dictionary on a zero-hit lookup. If the corpus
        is non-empty but no indexable token can be sampled from it, the warm
        cannot be proven effective, so preload RAISES (Codex R6) — the daemon
        then records index_preload_ok=False rather than implying a warm index.
        """
        start = time.perf_counter()
        warm_token, corpus_nonempty, token_is_real = await self._resolve_warm_token()
        if corpus_nonempty and not token_is_real:
            raise RuntimeError(
                "PgFtsBackend.preload: no indexable token found in sampled corpus; "
                "warm would be a zero-hit lookup that does not fault real postings"
            )

        matched = await self._run_warm(warm_token)
        # The warm token came from a live indexed chunk, so a correctly built,
        # fresh index MUST return at least one row. Zero matches means the index
        # is stale or its analyzer tokenizes differently than the sampler — the
        # warm did not fault real postings/heap/ranking pages, so do not report
        # success (Codex R7).
        if token_is_real and matched == 0:
            raise RuntimeError(
                "PgFtsBackend.preload: warm query matched no rows for a sampled "
                "corpus token; index is stale or analyzer-mismatched — not "
                "effectively warmed"
            )
        return (time.perf_counter() - start) * 1000

    async def _run_warm(self, warm_token: str) -> int:
        """Execute the ranked warm query and return the matched row count.

        Handles the BM25 → native fallback on a missing pg_search extension
        (Codex R4). Genuine errors propagate so the daemon records a failed
        preload.
        """
        if self._bm25_available is False:
            return await self._run_native_warm(warm_token)
        try:
            async with self._engine.connect() as conn:
                rows = (await conn.execute(self._bm25_warm_query(), {"q": warm_token})).fetchall()
            return len(rows)
        except (ProgrammingError, DBAPIError) as exc:
            if not _is_missing_bm25_error(exc):
                raise  # genuine error → observable to the daemon (Codex R4)
            # BM25 extension absent — mark for native fallback AND warm the
            # native path now so the first real query is not left cold. Use a
            # fresh connection: the failed BM25 query aborted this one's txn.
            self._bm25_available = False
            logger.info(
                "PgFtsBackend.preload: BM25 unavailable; warming native FTS fallback instead"
            )
            return await self._run_native_warm(warm_token)

    async def _run_native_warm(self, warm_token: str) -> int:
        fts_query = _native_fts_query(warm_token) or warm_token
        async with self._engine.connect() as conn:
            rows = (await conn.execute(self._native_warm_query(), {"q": fts_query})).fetchall()
        return len(rows)

    async def _resolve_warm_token(self) -> tuple[str, bool, bool]:
        """Resolve a warm token, sampling SEVERAL non-deleted indexed chunks so
        a real corpus token is found even if the first row is all stopwords
        (Codex R5/R6). Returns ``(token, corpus_nonempty, token_is_real)``:

        * ``token_is_real`` True  → token came from live indexed text; the warm
          is guaranteed to match the source row (effective).
        * ``corpus_nonempty`` True but ``token_is_real`` False → the corpus has
          rows yet none yielded an indexable token; the caller treats this as a
          failed warm rather than a zero-hit success.
        * ``corpus_nonempty`` False → genuinely empty corpus: nothing to warm,
          fixed fallback token, no effectiveness claim needed.

        Sampling errors are NOT swallowed (Codex R10): a transient failure must
        not be misread as "empty corpus" and let a zero-hit fixed-token warm
        report success. The exception propagates so the daemon records
        index_preload_ok=False (fail closed) — and the warm query, which hits
        the same tables, would fail anyway.
        """
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT c.chunk_text "
                        "FROM document_chunks c "
                        "JOIN file_paths fp ON c.path_id = fp.path_id "
                        "WHERE fp.deleted_at IS NULL AND c.chunk_text IS NOT NULL "
                        "LIMIT 20"
                    )
                )
            ).fetchall()
        if not rows:
            return _PRELOAD_WARM_TOKEN, False, False  # genuinely empty corpus
        for row in rows:
            if row[0]:
                tok = _first_corpus_token(str(row[0]))
                if tok:
                    return tok, True, True
        # Corpus has rows but no indexable token in the sample.
        return _PRELOAD_WARM_TOKEN, True, False

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
        *,
        timing: dict[str, float] | None = None,
    ) -> list[BaseSearchResult]:
        """BM25 chunk-level search.

        Returns up to *k* results ordered by BM25 score descending.
        All parameters are SQL-parameterized — no string interpolation.

        Args:
            query: BM25 search query string.
            path: Path prefix filter (e.g. "/zone/subdir/").
            k: Maximum number of results to return.
            zone_id: Zone isolation — only files in this zone are searched.
            timing: Optional dict to accumulate phase timings into. When
                provided, the wall-clock of the index-touching DB query is
                added to ``timing['index_load_ms']`` (Issue #4269). This
                isolates the BM25 scan / index page fault-in — where the cold
                network-volume read-stall lives — from surrounding Python work.

        Returns:
            List of BaseSearchResult ordered by score descending.
        """
        if self._bm25_available is False:
            return await self._keyword_search_native(query, path, k, zone_id, timing=timing)

        try:
            return await self._keyword_search_bm25(query, path, k, zone_id, timing=timing)
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
            return await self._keyword_search_native(query, path, k, zone_id, timing=timing)

    async def _keyword_search_bm25(
        self,
        query: str,
        path: str,
        k: int,
        zone_id: str,
        *,
        timing: dict[str, float] | None = None,
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
            index_load_start = time.perf_counter()
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
            _record_index_load(timing, index_load_start)

        return self._rows_to_results(rows, zone_id=zone_id)

    async def _keyword_search_native(
        self,
        query: str,
        path: str,
        k: int,
        zone_id: str,
        *,
        timing: dict[str, float] | None = None,
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
            index_load_start = time.perf_counter()
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
            _record_index_load(timing, index_load_start)

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
        """Indexed page-level search (one result per path, not per chunk).

        Pulls an over-fetched candidate set through ``keyword_search`` and
        max-pools those already-ranked chunk rows to page granularity. This
        preserves the #3980 rare-phrase behavior without a corpus-wide page
        aggregation CTE on the query hot path.

        Recall bound: only the top ``page_candidate_limit(k)`` chunk matches
        (``max(k * 8, 64)``) are considered. A page surfaces iff at least one
        of its chunks ranks within that candidate window. For pathologically
        long documents whose only match is a low-BM25 chunk ranked beyond the
        window, the page can be missed — the retired CTE path scored the whole
        page_text and had no such bound. Widen the multiplier if recall on
        very long documents regresses.

        Args:
            query: BM25 search query string.
            path: Path prefix filter (e.g. "/zone/subdir/").
            k: Maximum number of page-level results to return.
            zone_id: Zone isolation — only files in this zone are searched.

        Returns:
            List of BaseSearchResult (one per path) ordered by score desc.
        """
        candidates = await self.keyword_search(query, path, self.page_candidate_limit(k), zone_id)
        return self.page_results_from_chunks(candidates, k=k, zone_id=zone_id)

    def page_candidate_limit(self, k: int) -> int:
        return _page_candidate_limit(k)

    def page_results_from_chunks(
        self,
        chunks: Sequence[BaseSearchResult],
        *,
        k: int,
        zone_id: str,
    ) -> list[BaseSearchResult]:
        if k <= 0 or not chunks:
            return []

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
