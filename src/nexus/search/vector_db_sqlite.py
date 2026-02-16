"""SQLite backend for VectorDatabase (Issue #1520).

Handles sqlite-vec vector search and FTS5 keyword search.
Extracted from vector_db.py to isolate backend-specific logic.
"""

from __future__ import annotations

import logging
import struct
from typing import TYPE_CHECKING, Any

from sqlalchemy import event, text

from nexus.search.result_builders import build_result_from_row

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def init_sqlite(engine: Any, conn: Any) -> tuple[bool, bool]:
    """Initialize SQLite with sqlite-vec and FTS5.

    Args:
        engine: SQLAlchemy engine (for event listener registration).
        conn: Active database connection.

    Returns:
        Tuple of (vec_available, sqlite_vec_loaded).
    """
    vec_available = False
    sqlite_vec_loaded = False

    try:
        import sqlite_vec

        def _load_sqlite_vec(dbapi_conn: Any, connection_record: Any) -> None:  # noqa: ARG001
            """Load sqlite-vec extension on new connections."""
            dbapi_conn.enable_load_extension(True)
            sqlite_vec.load(dbapi_conn)
            dbapi_conn.enable_load_extension(False)

        event.listen(engine, "connect", _load_sqlite_vec)
        sqlite_vec_loaded = True
        vec_available = True

        # Load on current connection
        raw_conn = conn.connection.driver_connection
        raw_conn.enable_load_extension(True)
        sqlite_vec.load(raw_conn)
        raw_conn.enable_load_extension(False)

    except ImportError:
        import warnings

        warnings.warn(
            "sqlite-vec not installed. "
            "Only keyword search will be supported. "
            "For semantic/hybrid search, install: pip install sqlite-vec",
            stacklevel=3,
        )
    except Exception as e:
        import warnings

        warnings.warn(
            f"Failed to load sqlite-vec extension: {e}. "
            "Only keyword search will be supported. "
            "For semantic/hybrid search, install: pip install sqlite-vec",
            stacklevel=3,
        )

    # Add embedding column if not exists
    try:
        conn.execute(text("ALTER TABLE document_chunks ADD COLUMN embedding BLOB"))
        conn.commit()
    except Exception:
        # Column might already exist or table doesn't exist yet
        pass

    # Create FTS5 virtual table
    try:
        conn.execute(
            text("""
                CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts
                USING fts5(
                    chunk_id UNINDEXED,
                    chunk_text,
                    content='document_chunks',
                    content_rowid='rowid'
                )
            """)
        )
        conn.commit()
    except Exception:
        pass

    # Create FTS sync triggers
    for trigger_sql in [
        """CREATE TRIGGER IF NOT EXISTS document_chunks_fts_insert
           AFTER INSERT ON document_chunks BEGIN
               INSERT INTO document_chunks_fts(rowid, chunk_id, chunk_text)
               VALUES (new.rowid, new.chunk_id, new.chunk_text);
           END""",
        """CREATE TRIGGER IF NOT EXISTS document_chunks_fts_delete
           AFTER DELETE ON document_chunks BEGIN
               DELETE FROM document_chunks_fts WHERE rowid = old.rowid;
           END""",
        """CREATE TRIGGER IF NOT EXISTS document_chunks_fts_update
           AFTER UPDATE ON document_chunks BEGIN
               DELETE FROM document_chunks_fts WHERE rowid = old.rowid;
               INSERT INTO document_chunks_fts(rowid, chunk_id, chunk_text)
               VALUES (new.rowid, new.chunk_id, new.chunk_text);
           END""",
    ]:
        try:
            conn.execute(text(trigger_sql))
            conn.commit()
        except Exception:
            pass

    return vec_available, sqlite_vec_loaded


def reload_sqlite_vec(conn: Any) -> None:
    """Reload sqlite-vec on an existing connection (already registered listener)."""
    try:
        import sqlite_vec

        raw_conn = conn.connection.driver_connection
        raw_conn.enable_load_extension(True)
        sqlite_vec.load(raw_conn)
        raw_conn.enable_load_extension(False)
    except (AttributeError, ImportError, RuntimeError):
        pass


def sqlite_store_embedding(session: Session, chunk_id: str, embedding: list[float]) -> None:
    """Store embedding as BLOB in SQLite."""
    blob = struct.pack(f"{len(embedding)}f", *embedding)
    session.execute(
        text("UPDATE document_chunks SET embedding = :embedding WHERE chunk_id = :chunk_id"),
        {"embedding": blob, "chunk_id": chunk_id},
    )


def sqlite_vector_search(
    session: Session,
    embedding: list[float],
    limit: int,
    path_filter: str | None,
) -> list[dict[str, Any]]:
    """SQLite vector search using sqlite-vec."""
    query_blob = struct.pack(f"{len(embedding)}f", *embedding)

    if path_filter:
        query = text("""
            SELECT c.chunk_id, c.chunk_index, c.chunk_text,
                   c.start_offset, c.end_offset, c.line_start, c.line_end,
                   fp.virtual_path,
                   (1 - vec_distance_cosine(c.embedding, :embedding)) as score
            FROM document_chunks c
            JOIN file_paths fp ON c.path_id = fp.path_id
            WHERE c.embedding IS NOT NULL AND fp.virtual_path LIKE :path_filter
            ORDER BY vec_distance_cosine(c.embedding, :embedding) ASC
            LIMIT :limit
        """)
        results = session.execute(
            query, {"embedding": query_blob, "limit": limit, "path_filter": f"{path_filter}%"}
        )
    else:
        query = text("""
            SELECT c.chunk_id, c.chunk_index, c.chunk_text,
                   c.start_offset, c.end_offset, c.line_start, c.line_end,
                   fp.virtual_path,
                   (1 - vec_distance_cosine(c.embedding, :embedding)) as score
            FROM document_chunks c
            JOIN file_paths fp ON c.path_id = fp.path_id
            WHERE c.embedding IS NOT NULL
            ORDER BY vec_distance_cosine(c.embedding, :embedding) ASC
            LIMIT :limit
        """)
        results = session.execute(query, {"embedding": query_blob, "limit": limit})

    return [build_result_from_row(row) for row in results]


def sqlite_keyword_search(
    session: Session,
    query: str,
    limit: int,
    path_filter: str | None,
) -> list[dict[str, Any]]:
    """SQLite keyword search using FTS5."""
    if path_filter:
        sql = text("""
            SELECT c.chunk_id, c.chunk_index, c.chunk_text,
                   c.start_offset, c.end_offset, c.line_start, c.line_end,
                   fp.virtual_path, fts.rank as score
            FROM document_chunks_fts fts
            JOIN document_chunks c ON c.chunk_id = fts.chunk_id
            JOIN file_paths fp ON c.path_id = fp.path_id
            WHERE fts.chunk_text MATCH :query AND fp.virtual_path LIKE :path_filter
            ORDER BY fts.rank
            LIMIT :limit
        """)
        results = session.execute(
            sql, {"query": query, "limit": limit, "path_filter": f"{path_filter}%"}
        )
    else:
        sql = text("""
            SELECT c.chunk_id, c.chunk_index, c.chunk_text,
                   c.start_offset, c.end_offset, c.line_start, c.line_end,
                   fp.virtual_path, fts.rank as score
            FROM document_chunks_fts fts
            JOIN document_chunks c ON c.chunk_id = fts.chunk_id
            JOIN file_paths fp ON c.path_id = fp.path_id
            WHERE fts.chunk_text MATCH :query
            ORDER BY fts.rank
            LIMIT :limit
        """)
        results = session.execute(sql, {"query": query, "limit": limit})

    return [build_result_from_row(row, score_abs=True) for row in results]
