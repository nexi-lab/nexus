"""PostgreSQL backend for VectorDatabase (Issue #1520).

Handles pgvector vector search, pg_textsearch BM25, and ts_rank fallback.
Extracted from vector_db.py to isolate backend-specific logic.
"""

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

from nexus.bricks.search.result_builders import build_result_from_row

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def init_postgresql(conn: Any, hnsw_config: Any) -> tuple[bool, bool]:
    """Initialize PostgreSQL with pgvector and pg_textsearch.

    Args:
        conn: Active database connection.
        hnsw_config: HNSWConfig instance for index parameters.

    Returns:
        Tuple of (vec_available, bm25_available).
    """
    # Try pgvector extension
    vec_available = False
    try:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
        vec_available = True
    except (OperationalError, ProgrammingError, OSError, RuntimeError) as e:
        import warnings

        warnings.warn(
            "pgvector extension not available. "
            "Only keyword search will be supported. "
            "For semantic/hybrid search, install pgvector: "
            "https://github.com/pgvector/pgvector",
            stacklevel=3,
        )
        logger.debug("pgvector init failed: %s", e)
        conn.rollback()

    # Try pg_textsearch BM25 (PostgreSQL 17+)
    bm25_available = False
    try:
        result = conn.execute(text("SHOW server_version_num"))
        version_num = int(result.scalar())

        if version_num >= 170000:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_textsearch"))
            conn.commit()
            result = conn.execute(text("SELECT 1 FROM pg_am WHERE amname = 'bm25' LIMIT 1"))
            if result.scalar():
                bm25_available = True
                logger.info("pg_textsearch BM25 extension initialized")
    except (OperationalError, ProgrammingError) as e:
        logger.debug("pg_textsearch not available: %s. Using ts_rank fallback.", e)
        conn.rollback()
    except Exception as e:
        logger.debug("pg_textsearch init error: %s", e)
        conn.rollback()

    # Add embedding column if pgvector available
    if vec_available:
        try:
            conn.execute(text("ALTER TABLE document_chunks ADD COLUMN embedding halfvec(384)"))
            conn.commit()
        except (OperationalError, ProgrammingError):
            conn.rollback()

    # GIN index for text search
    try:
        conn.execute(
            text("""
                CREATE INDEX IF NOT EXISTS idx_chunks_text_search
                ON document_chunks
                USING GIN (to_tsvector('english', chunk_text))
            """)
        )
        conn.commit()
    except (OperationalError, ProgrammingError):
        conn.rollback()

    # HNSW index for vector search
    if vec_available:
        try:
            index_sql = hnsw_config.get_create_index_sql(
                table="document_chunks",
                column="embedding",
                index_name="idx_chunks_embedding_hnsw",
                operator_class="halfvec_cosine_ops",
            )
            conn.execute(text(index_sql))
            conn.commit()
            logger.info(
                "HNSW index created with m=%d, ef_construction=%d",
                hnsw_config.m,
                hnsw_config.ef_construction,
            )
        except (OperationalError, ProgrammingError):
            conn.rollback()

    return vec_available, bm25_available


def postgres_store_embedding(session: "Session", chunk_id: str, embedding: list[float]) -> None:
    """Store embedding as pgvector array."""
    session.execute(
        text("UPDATE document_chunks SET embedding = :embedding WHERE chunk_id = :chunk_id"),
        {"embedding": embedding, "chunk_id": chunk_id},
    )


def postgres_vector_search(
    session: "Session",
    embedding: list[float],
    limit: int,
    path_filter: str | None,
    hnsw_config: Any,
) -> list[dict[str, Any]]:
    """PostgreSQL vector search using pgvector."""
    hnsw_config.apply_search_settings(session)

    if path_filter:
        query = text("""
            SELECT c.chunk_id, c.chunk_index, c.chunk_text,
                   c.start_offset, c.end_offset, c.line_start, c.line_end,
                   fp.virtual_path,
                   1 - (c.embedding <=> CAST(:embedding AS halfvec)) as score
            FROM document_chunks c
            JOIN file_paths fp ON c.path_id = fp.path_id
            WHERE c.embedding IS NOT NULL AND fp.virtual_path LIKE :path_filter
            ORDER BY c.embedding <=> CAST(:embedding AS halfvec)
            LIMIT :limit
        """)
        results = session.execute(
            query, {"embedding": embedding, "limit": limit, "path_filter": f"{path_filter}%"}
        )
    else:
        query = text("""
            SELECT c.chunk_id, c.chunk_index, c.chunk_text,
                   c.start_offset, c.end_offset, c.line_start, c.line_end,
                   fp.virtual_path,
                   1 - (c.embedding <=> CAST(:embedding AS halfvec)) as score
            FROM document_chunks c
            JOIN file_paths fp ON c.path_id = fp.path_id
            WHERE c.embedding IS NOT NULL
            ORDER BY c.embedding <=> CAST(:embedding AS halfvec)
            LIMIT :limit
        """)
        results = session.execute(query, {"embedding": embedding, "limit": limit})

    return [build_result_from_row(row) for row in results]


def postgres_keyword_search(
    session: "Session",
    query: str,
    limit: int,
    path_filter: str | None,
    bm25_available: bool,
) -> list[dict[str, Any]]:
    """PostgreSQL keyword search (BM25 or ts_rank fallback)."""
    if bm25_available:
        return _postgres_bm25_search(session, query, limit, path_filter)
    return _postgres_tsrank_search(session, query, limit, path_filter)


def _postgres_bm25_search(
    session: "Session",
    query: str,
    limit: int,
    path_filter: str | None,
) -> list[dict[str, Any]]:
    """PostgreSQL BM25 search using pg_textsearch."""
    if path_filter:
        sql = text("""
            SELECT c.chunk_id, c.chunk_index, c.chunk_text,
                   c.start_offset, c.end_offset, c.line_start, c.line_end,
                   fp.virtual_path,
                   c.chunk_text <@> to_bm25query(:query, 'idx_chunks_bm25') as score
            FROM document_chunks c
            JOIN file_paths fp ON c.path_id = fp.path_id
            WHERE fp.virtual_path LIKE :path_filter
            ORDER BY c.chunk_text <@> to_bm25query(:query, 'idx_chunks_bm25')
            LIMIT :limit
        """)
        results = session.execute(
            sql, {"query": query, "limit": limit, "path_filter": f"{path_filter}%"}
        )
    else:
        sql = text("""
            SELECT c.chunk_id, c.chunk_index, c.chunk_text,
                   c.start_offset, c.end_offset, c.line_start, c.line_end,
                   fp.virtual_path,
                   c.chunk_text <@> to_bm25query(:query, 'idx_chunks_bm25') as score
            FROM document_chunks c
            JOIN file_paths fp ON c.path_id = fp.path_id
            ORDER BY c.chunk_text <@> to_bm25query(:query, 'idx_chunks_bm25')
            LIMIT :limit
        """)
        results = session.execute(sql, {"query": query, "limit": limit})

    return [build_result_from_row(row, score_abs=True) for row in results]


def _postgres_tsrank_search(
    session: "Session",
    query: str,
    limit: int,
    path_filter: str | None,
) -> list[dict[str, Any]]:
    """PostgreSQL ts_rank fallback (pre-PG17 or no pg_textsearch)."""
    if path_filter:
        sql = text("""
            SELECT c.chunk_id, c.chunk_index, c.chunk_text,
                   c.start_offset, c.end_offset, c.line_start, c.line_end,
                   fp.virtual_path,
                   ts_rank(to_tsvector('english', c.chunk_text),
                           plainto_tsquery('english', :query)) as score
            FROM document_chunks c
            JOIN file_paths fp ON c.path_id = fp.path_id
            WHERE to_tsvector('english', c.chunk_text) @@ plainto_tsquery('english', :query)
              AND fp.virtual_path LIKE :path_filter
            ORDER BY score DESC
            LIMIT :limit
        """)
        results = session.execute(
            sql, {"query": query, "limit": limit, "path_filter": f"{path_filter}%"}
        )
    else:
        sql = text("""
            SELECT c.chunk_id, c.chunk_index, c.chunk_text,
                   c.start_offset, c.end_offset, c.line_start, c.line_end,
                   fp.virtual_path,
                   ts_rank(to_tsvector('english', c.chunk_text),
                           plainto_tsquery('english', :query)) as score
            FROM document_chunks c
            JOIN file_paths fp ON c.path_id = fp.path_id
            WHERE to_tsvector('english', c.chunk_text) @@ plainto_tsquery('english', :query)
            ORDER BY score DESC
            LIMIT :limit
        """)
        results = session.execute(sql, {"query": query, "limit": limit})

    return [build_result_from_row(row) for row in results]
