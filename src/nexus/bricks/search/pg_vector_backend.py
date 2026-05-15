"""Postgres pgvector backend (Issue #3699).

Wraps the existing ``idx_chunks_embedding_hnsw`` index on
``document_chunks.embedding halfvec(1536)`` for cosine-distance KNN queries.

The donor SQL at ``daemon.py:2010-2021`` performs the same halfvec cosine
search inline; this class extracts it into a reusable backend conforming to
the SearchBackend protocol (T1).

Writes are NOT owned by this backend. ChunkStore.replace_document_chunks
already writes the ``embedding`` column atomically; pgvector maintains the
HNSW index automatically. The add/upsert/delete methods are stubbed as
NotImplementedError until T9 (daemon integration) wires the call site
through ChunkStore's actual API. The stubs satisfy the SearchBackend
protocol shape so isinstance() checks pass immediately.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from nexus.bricks.search.results import BaseSearchResult

# ---------------------------------------------------------------------------
# SQL template
# ---------------------------------------------------------------------------
# Uses halfvec cosine distance (<=>). The CAST(:qvec AS halfvec) converts the
# '[0.1,0.2,...]' string representation emitted by str(list(query_vector))
# to a pgvector halfvec value. No dimension suffix is required in the CAST —
# Postgres infers it from the column type.
# ---------------------------------------------------------------------------
_SEMANTIC_SQL = text("""
    SELECT c.chunk_id,
           fp.virtual_path AS path,
           c.chunk_text,
           c.chunk_index,
           1 - (c.embedding <=> CAST(:qvec AS halfvec)) AS score
    FROM document_chunks c
    JOIN file_paths fp ON c.path_id = fp.path_id
    WHERE c.embedding IS NOT NULL
      AND fp.zone_id = :zone_id
      AND fp.virtual_path LIKE :prefix || '%'
      AND fp.deleted_at IS NULL
    ORDER BY c.embedding <=> CAST(:qvec AS halfvec)
    LIMIT :k
""")


class PgVectorBackend:
    """Postgres vector search backend using pgvector halfvec(1536) HNSW.

    Satisfies the SearchBackend protocol (T1). Reads use the existing
    ``idx_chunks_embedding_hnsw`` index on ``document_chunks.embedding``.
    Writes are not owned here — see module docstring.

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

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def startup(self) -> None:
        """No-op: pgvector HNSW index is maintained by Postgres automatically."""
        return None

    async def shutdown(self) -> None:
        """No-op: engine disposal is the caller's responsibility."""
        return None

    # -------------------------------------------------------------------------
    # Write pass-through stubs (T9 wires these to ChunkStore)
    # -------------------------------------------------------------------------
    # ChunkStore exposes replace_document_chunks(path_id, chunks) and
    # delete_document_chunks(path_id) — a different signature than the
    # protocol's add/upsert/delete(ids). The daemon (T9) will own the mapping.
    # Until then, these stubs keep the protocol satisfied at the isinstance()
    # level.

    async def add(self, docs: Sequence[dict[str, Any]], *, zone_id: str) -> int:
        # TODO(T9): delegate to chunk_store.replace_document_chunks
        raise NotImplementedError(
            "PgVectorBackend.add: write path is owned by ChunkStore. "
            "Wire through daemon integration (T9)."
        )

    async def upsert(self, docs: Sequence[dict[str, Any]], *, zone_id: str) -> int:
        # TODO(T9): delegate to chunk_store.replace_document_chunks (idempotent)
        raise NotImplementedError(
            "PgVectorBackend.upsert: write path is owned by ChunkStore. "
            "Wire through daemon integration (T9)."
        )

    async def delete(self, ids: Sequence[str], *, zone_id: str) -> int:
        # TODO(T9): delegate to chunk_store.delete_document_chunks
        raise NotImplementedError(
            "PgVectorBackend.delete: write path is owned by ChunkStore. "
            "Wire through daemon integration (T9)."
        )

    # -------------------------------------------------------------------------
    # Keyword search — no-op (lives in PgFtsBackend, T5)
    # -------------------------------------------------------------------------

    async def keyword_search(
        self,
        query: str,  # noqa: ARG002
        path: str,  # noqa: ARG002
        k: int,  # noqa: ARG002
        zone_id: str,  # noqa: ARG002
    ) -> list[BaseSearchResult]:
        """Not implemented in this backend — keyword search lives in PgFtsBackend (T5)."""
        return []

    # -------------------------------------------------------------------------
    # Semantic search — halfvec cosine KNN
    # -------------------------------------------------------------------------

    async def semantic_search(
        self,
        query_vector: Sequence[float],
        path: str,
        k: int,
        zone_id: str,
    ) -> list[BaseSearchResult]:
        """Cosine KNN over ``document_chunks.embedding halfvec(1536)``.

        Uses the ``idx_chunks_embedding_hnsw`` index. Chunks with a NULL
        embedding are excluded by the ``c.embedding IS NOT NULL`` predicate.

        Args:
            query_vector: 1536-dim query embedding as a Python sequence of
                floats. Passed to SQL as a bracketed string so pgvector can
                CAST it to halfvec (e.g. ``'[0.1, 0.2, ...]'::halfvec``).
            path: Path prefix filter (e.g. ``"/zone/subdir/"``). Only chunks
                whose ``file_paths.virtual_path`` starts with this prefix are
                considered.
            k: Maximum number of results to return.
            zone_id: Zone isolation — only files in this zone are searched.

        Returns:
            List of BaseSearchResult ordered by cosine similarity descending
            (nearest neighbours first). ``vector_score`` is populated with the
            cosine similarity (1 − distance).
        """
        # Empty-vector guard: pgvector rejects ``CAST('[]' AS halfvec)`` with
        # ``zero-length vector not allowed`` (and a 1536-dim halfvec can't
        # accept a 0-length input regardless). Fail fast so callers that
        # forgot to embed the query get an empty list rather than a 500.
        if not query_vector:
            return []
        # pgvector expects '[0.1,0.2,...]' string for halfvec CAST.
        qvec_str = str(list(query_vector))

        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        _SEMANTIC_SQL,
                        {
                            "qvec": qvec_str,
                            "zone_id": zone_id,
                            "prefix": path,
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
                chunk_text=r["chunk_text"],
                score=float(r["score"]),
                chunk_index=int(r["chunk_index"]),
                vector_score=float(r["score"]),
                zone_id=zone_id,
            )
            for r in rows
        ]
