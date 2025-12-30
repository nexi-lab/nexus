"""Vector database integration using sqlite-vec and pgvector.

Provides vector search capabilities using native database extensions:
- SQLite: sqlite-vec extension for vectors, FTS5 for keywords
- PostgreSQL: pgvector for vectors, pg_textsearch BM25 for keywords (PG17+)

BM25 ranking (pg_textsearch):
- True BM25 ranking with IDF, term frequency saturation, length normalization
- 3x faster than Elasticsearch, ~10ms vs ts_rank's 25-30s at 800K rows
- Falls back to ts_rank() on PostgreSQL < 17 or when extension unavailable

Zoekt integration:
- keyword_search tries Zoekt first for fast candidate retrieval
- Falls back to FTS/BM25 if Zoekt unavailable
"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import TYPE_CHECKING, Any

from sqlalchemy import event, text

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session


class VectorDatabase:
    """Vector database using sqlite-vec or pgvector based on database type."""

    def __init__(self, engine: Engine):
        """Initialize vector database.

        Args:
            engine: SQLAlchemy engine
        """
        self.engine = engine
        self.db_type = engine.dialect.name
        self._initialized = False
        self.vec_available = False  # Set to True if vector extension is loaded
        self.bm25_available = False  # Set to True if pg_textsearch BM25 is available
        self._sqlite_vec_loaded = False  # Track if we've set up the event listener

    def initialize(self) -> None:
        """Initialize vector extensions and create FTS tables."""
        if self._initialized:
            return

        with self.engine.connect() as conn:
            if self.db_type == "sqlite":
                self._init_sqlite(conn)
            elif self.db_type == "postgresql":
                self._init_postgresql(conn)
            else:
                raise ValueError(f"Unsupported database type: {self.db_type}")

        self._initialized = True

    def _init_sqlite(self, conn: Any) -> None:
        """Initialize SQLite with sqlite-vec and FTS5.

        Args:
            conn: Database connection
        """
        # Set up event listener to load sqlite-vec on every connection
        if not self._sqlite_vec_loaded:
            vec_available = False
            try:
                import sqlite_vec

                # Define a function to load sqlite-vec on new connections
                def _load_sqlite_vec(dbapi_conn: Any, connection_record: Any) -> None:  # noqa: ARG001
                    """Load sqlite-vec extension on new connections."""
                    dbapi_conn.enable_load_extension(True)
                    sqlite_vec.load(dbapi_conn)
                    dbapi_conn.enable_load_extension(False)

                # Register the event listener
                event.listen(self.engine, "connect", _load_sqlite_vec)
                self._sqlite_vec_loaded = True
                vec_available = True

                # Also load it on the current connection
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
                    stacklevel=2,
                )
            except Exception as e:
                import warnings

                warnings.warn(
                    f"Failed to load sqlite-vec extension: {e}. "
                    "Only keyword search will be supported. "
                    "For semantic/hybrid search, install: pip install sqlite-vec",
                    stacklevel=2,
                )

            self.vec_available = vec_available
        else:
            # Already set up the listener, just load on current connection
            try:
                import sqlite_vec

                raw_conn = conn.connection.driver_connection
                raw_conn.enable_load_extension(True)
                sqlite_vec.load(raw_conn)
                raw_conn.enable_load_extension(False)
            except (AttributeError, ImportError, RuntimeError):
                # Ignore errors: extension might already be loaded or not available
                pass

        # Add embedding column if not exists
        try:
            conn.execute(text("ALTER TABLE document_chunks ADD COLUMN embedding BLOB"))
            conn.commit()
        except Exception:
            # Column might already exist (duplicate column error) or table doesn't exist yet
            # If table doesn't exist, it will be created by the metadata store
            pass

        # Create FTS5 virtual table for keyword search
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
            # Table might already exist or base table doesn't exist yet
            pass

        # Create triggers to keep FTS in sync
        try:
            conn.execute(
                text("""
                CREATE TRIGGER IF NOT EXISTS document_chunks_fts_insert
                AFTER INSERT ON document_chunks BEGIN
                    INSERT INTO document_chunks_fts(rowid, chunk_id, chunk_text)
                    VALUES (new.rowid, new.chunk_id, new.chunk_text);
                END
            """)
            )
            conn.execute(
                text("""
                CREATE TRIGGER IF NOT EXISTS document_chunks_fts_delete
                AFTER DELETE ON document_chunks BEGIN
                    DELETE FROM document_chunks_fts WHERE rowid = old.rowid;
                END
            """)
            )
            conn.execute(
                text("""
                CREATE TRIGGER IF NOT EXISTS document_chunks_fts_update
                AFTER UPDATE ON document_chunks BEGIN
                    DELETE FROM document_chunks_fts WHERE rowid = old.rowid;
                    INSERT INTO document_chunks_fts(rowid, chunk_id, chunk_text)
                    VALUES (new.rowid, new.chunk_id, new.chunk_text);
                END
            """)
            )
            conn.commit()
        except Exception:
            # Triggers might already exist or base table doesn't exist yet
            pass

    def _init_postgresql(self, conn: Any) -> None:
        """Initialize PostgreSQL with pgvector and pg_textsearch.

        Args:
            conn: Database connection
        """
        # Try to create pgvector extension (optional - only needed for semantic search)
        vec_available = False
        try:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
            vec_available = True
        except (OSError, RuntimeError, Exception):
            # pgvector not available - will only support keyword search
            # Catches psycopg2.errors.FeatureNotSupported and other database errors
            import warnings

            warnings.warn(
                "pgvector extension not available. "
                "Only keyword search will be supported. "
                "For semantic/hybrid search, install pgvector: "
                "https://github.com/pgvector/pgvector",
                stacklevel=2,
            )
            # Rollback the failed transaction so subsequent commands can execute
            conn.rollback()

        self.vec_available = vec_available

        # Try to create pg_textsearch extension (optional - for true BM25 ranking)
        # Requires PostgreSQL 17+
        bm25_available = False
        try:
            # Check PostgreSQL version first
            result = conn.execute(text("SHOW server_version_num"))
            version_num = int(result.scalar())

            if version_num >= 170000:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_textsearch"))
                conn.commit()
                # Verify the extension is loaded by checking for the bm25 index type
                result = conn.execute(text("SELECT 1 FROM pg_am WHERE amname = 'bm25' LIMIT 1"))
                if result.scalar():
                    bm25_available = True
                    logger.info("pg_textsearch BM25 extension initialized")
        except Exception as e:
            # pg_textsearch not available - will use ts_rank fallback
            logger.debug(f"pg_textsearch not available: {e}. Using ts_rank fallback.")
            conn.rollback()

        self.bm25_available = bm25_available

        # Add embedding column if pgvector is available
        if vec_available:
            # Note: Dimension will be set dynamically based on model
            try:
                conn.execute(text("ALTER TABLE document_chunks ADD COLUMN embedding halfvec(1536)"))
                conn.commit()
            except Exception:
                # Column might already exist (duplicate column error) - rollback and continue
                conn.rollback()

        # Create GIN index for text search
        try:
            conn.execute(
                text("""
                CREATE INDEX IF NOT EXISTS idx_chunks_text_search
                ON document_chunks
                USING GIN (to_tsvector('english', chunk_text))
            """)
            )
            conn.commit()
        except Exception:
            # Index might already exist - rollback and continue
            conn.rollback()

        # Create HNSW index for vector search (only if pgvector available)
        # Tuned for 100K+ vectors with 1536 dimensions (OpenAI embeddings)
        # - m=24: More connections for high-dimensional data, improves recall
        # - ef_construction=128: Better graph quality at build time
        # See: https://github.com/nexi-lab/nexus/issues/947
        if vec_available:
            try:
                conn.execute(
                    text("""
                    CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw
                    ON document_chunks
                    USING hnsw (embedding halfvec_cosine_ops)
                    WITH (m = 24, ef_construction = 128)
                """)
                )
                conn.commit()
            except Exception:
                # Index might already exist or other pgvector-related error
                # Rollback transaction to avoid InFailedSqlTransaction errors
                conn.rollback()

    def store_embedding(self, session: Session, chunk_id: str, embedding: list[float]) -> None:
        """Store embedding for a chunk.

        Args:
            session: Database session
            chunk_id: Chunk ID
            embedding: Embedding vector
        """
        if self.db_type == "sqlite":
            # Serialize to BLOB (float32 array)
            blob = struct.pack(f"{len(embedding)}f", *embedding)
            session.execute(
                text(
                    "UPDATE document_chunks SET embedding = :embedding WHERE chunk_id = :chunk_id"
                ),
                {"embedding": blob, "chunk_id": chunk_id},
            )
        elif self.db_type == "postgresql":
            # pgvector handles array directly
            session.execute(
                text(
                    "UPDATE document_chunks SET embedding = :embedding WHERE chunk_id = :chunk_id"
                ),
                {"embedding": embedding, "chunk_id": chunk_id},
            )

    def vector_search(
        self,
        session: Session,
        query_embedding: list[float],
        limit: int = 10,
        path_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search by vector similarity.

        Args:
            session: Database session
            query_embedding: Query embedding vector
            limit: Maximum number of results
            path_filter: Optional path prefix filter

        Returns:
            List of search results with scores
        """
        if self.db_type == "sqlite":
            return self._sqlite_vector_search(session, query_embedding, limit, path_filter)
        elif self.db_type == "postgresql":
            return self._postgres_vector_search(session, query_embedding, limit, path_filter)
        else:
            raise ValueError(f"Unsupported database type: {self.db_type}")

    def _sqlite_vector_search(
        self, session: Session, embedding: list[float], limit: int, path_filter: str | None
    ) -> list[dict[str, Any]]:
        """SQLite vector search using sqlite-vec.

        Args:
            session: Database session
            embedding: Query embedding
            limit: Max results
            path_filter: Path filter

        Returns:
            Search results
        """
        # Serialize embedding to BLOB
        query_blob = struct.pack(f"{len(embedding)}f", *embedding)

        if path_filter:
            query = text("""
                SELECT
                    c.chunk_id,
                    c.path_id,
                    c.chunk_index,
                    c.chunk_text,
                    c.start_offset,
                    c.end_offset,
                    c.line_start,
                    c.line_end,
                    fp.virtual_path,
                    vec_distance_cosine(c.embedding, :embedding) as distance,
                    (1 - vec_distance_cosine(c.embedding, :embedding)) as score
                FROM document_chunks c
                JOIN file_paths fp ON c.path_id = fp.path_id
                WHERE c.embedding IS NOT NULL
                  AND fp.virtual_path LIKE :path_filter
                ORDER BY distance ASC
                LIMIT :limit
            """)
            results = session.execute(
                query,
                {"embedding": query_blob, "limit": limit, "path_filter": f"{path_filter}%"},
            )
        else:
            query = text("""
                SELECT
                    c.chunk_id,
                    c.path_id,
                    c.chunk_index,
                    c.chunk_text,
                    c.start_offset,
                    c.end_offset,
                    c.line_start,
                    c.line_end,
                    fp.virtual_path,
                    vec_distance_cosine(c.embedding, :embedding) as distance,
                    (1 - vec_distance_cosine(c.embedding, :embedding)) as score
                FROM document_chunks c
                JOIN file_paths fp ON c.path_id = fp.path_id
                WHERE c.embedding IS NOT NULL
                ORDER BY distance ASC
                LIMIT :limit
            """)
            results = session.execute(query, {"embedding": query_blob, "limit": limit})

        return [
            {
                "chunk_id": row.chunk_id,
                "path": row.virtual_path,
                "chunk_index": row.chunk_index,
                "chunk_text": row.chunk_text,
                "start_offset": row.start_offset,
                "end_offset": row.end_offset,
                "line_start": row.line_start,
                "line_end": row.line_end,
                "score": float(row.score),
            }
            for row in results
        ]

    def _postgres_vector_search(
        self, session: Session, embedding: list[float], limit: int, path_filter: str | None
    ) -> list[dict[str, Any]]:
        """PostgreSQL vector search using pgvector.

        Args:
            session: Database session
            embedding: Query embedding
            limit: Max results
            path_filter: Path filter

        Returns:
            Search results
        """
        # Set ef_search for better recall (default is 40, we use 100 for ~0.998 recall)
        # Using SET LOCAL to only affect current transaction
        # See: https://github.com/nexi-lab/nexus/issues/947
        session.execute(text("SET LOCAL hnsw.ef_search = 100"))

        if path_filter:
            query = text("""
                SELECT
                    c.chunk_id,
                    c.path_id,
                    c.chunk_index,
                    c.chunk_text,
                    c.start_offset,
                    c.end_offset,
                    c.line_start,
                    c.line_end,
                    fp.virtual_path,
                    1 - (c.embedding <=> CAST(:embedding AS halfvec)) as score
                FROM document_chunks c
                JOIN file_paths fp ON c.path_id = fp.path_id
                WHERE c.embedding IS NOT NULL
                  AND fp.virtual_path LIKE :path_filter
                ORDER BY c.embedding <=> CAST(:embedding AS halfvec)
                LIMIT :limit
            """)
            results = session.execute(
                query,
                {"embedding": embedding, "limit": limit, "path_filter": f"{path_filter}%"},
            )
        else:
            query = text("""
                SELECT
                    c.chunk_id,
                    c.path_id,
                    c.chunk_index,
                    c.chunk_text,
                    c.start_offset,
                    c.end_offset,
                    c.line_start,
                    c.line_end,
                    fp.virtual_path,
                    1 - (c.embedding <=> CAST(:embedding AS halfvec)) as score
                FROM document_chunks c
                JOIN file_paths fp ON c.path_id = fp.path_id
                WHERE c.embedding IS NOT NULL
                ORDER BY c.embedding <=> CAST(:embedding AS halfvec)
                LIMIT :limit
            """)
            results = session.execute(query, {"embedding": embedding, "limit": limit})

        return [
            {
                "chunk_id": row.chunk_id,
                "path": row.virtual_path,
                "chunk_index": row.chunk_index,
                "chunk_text": row.chunk_text,
                "start_offset": row.start_offset,
                "end_offset": row.end_offset,
                "line_start": row.line_start,
                "line_end": row.line_end,
                "score": float(row.score),
            }
            for row in results
        ]

    def keyword_search(
        self, session: Session, query: str, limit: int = 10, path_filter: str | None = None
    ) -> list[dict[str, Any]]:
        """Search by keywords using Zoekt (if available) or FTS.

        Tries Zoekt first for fast trigram-based search, falls back to FTS.

        Args:
            session: Database session
            query: Search query
            limit: Maximum number of results
            path_filter: Optional path prefix filter

        Returns:
            List of search results with scores
        """
        # Try Zoekt first for accelerated search
        zoekt_results = self._try_keyword_search_with_zoekt(query, limit, path_filter)
        if zoekt_results is not None:
            logger.debug(f"[KEYWORD] Zoekt returned {len(zoekt_results)} results")
            return zoekt_results

        # Fall back to FTS
        logger.debug("[KEYWORD] Using FTS fallback")
        if self.db_type == "sqlite":
            return self._sqlite_keyword_search(session, query, limit, path_filter)
        elif self.db_type == "postgresql":
            return self._postgres_keyword_search(session, query, limit, path_filter)
        else:
            raise ValueError(f"Unsupported database type: {self.db_type}")

    def _try_keyword_search_with_zoekt(
        self, query: str, limit: int, path_filter: str | None
    ) -> list[dict[str, Any]] | None:
        """Try to use Zoekt for keyword search.

        Args:
            query: Search query
            limit: Maximum results
            path_filter: Optional path prefix

        Returns:
            List of results if Zoekt succeeded, None to fall back to FTS
        """
        try:
            from nexus.search.zoekt_client import get_zoekt_client
        except ImportError:
            return None

        client = get_zoekt_client()

        # Check if Zoekt is available (sync wrapper)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # In async context, can't use run_until_complete
                return None
            is_available = loop.run_until_complete(client.is_available())
        except RuntimeError:
            is_available = asyncio.run(client.is_available())

        if not is_available:
            return None

        logger.debug("[KEYWORD] Using Zoekt for accelerated search")

        try:
            # Build Zoekt query
            zoekt_query = query
            if path_filter:
                zoekt_query = f"file:{path_filter.lstrip('/')} {zoekt_query}"

            # Run search
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    return None
                matches = loop.run_until_complete(client.search(zoekt_query, num=limit * 2))
            except RuntimeError:
                matches = asyncio.run(client.search(zoekt_query, num=limit * 2))

            if not matches:
                # No results - let FTS try
                return None

            # Convert Zoekt results to keyword_search format
            results = []
            for match in matches[:limit]:
                results.append(
                    {
                        "chunk_id": None,  # Not from chunks table
                        "path_id": None,
                        "chunk_index": 0,
                        "chunk_text": match.content,
                        "start_offset": 0,
                        "end_offset": len(match.content),
                        "line_start": match.line,
                        "line_end": match.line,
                        "virtual_path": match.file,
                        "score": match.score or 1.0,
                    }
                )

            logger.debug(f"[KEYWORD] Zoekt: {len(matches)} matches, returning {len(results)}")
            return results

        except Exception as e:
            logger.warning(f"[KEYWORD] Zoekt search failed, falling back to FTS: {e}")
            return None

    def _sqlite_keyword_search(
        self, session: Session, query: str, limit: int, path_filter: str | None
    ) -> list[dict[str, Any]]:
        """SQLite keyword search using FTS5.

        Args:
            session: Database session
            query: Search query
            limit: Max results
            path_filter: Path filter

        Returns:
            Search results
        """
        if path_filter:
            sql = text("""
                SELECT
                    c.chunk_id,
                    c.path_id,
                    c.chunk_index,
                    c.chunk_text,
                    c.start_offset,
                    c.end_offset,
                    c.line_start,
                    c.line_end,
                    fp.virtual_path,
                    fts.rank as score
                FROM document_chunks_fts fts
                JOIN document_chunks c ON c.chunk_id = fts.chunk_id
                JOIN file_paths fp ON c.path_id = fp.path_id
                WHERE fts.chunk_text MATCH :query
                  AND fp.virtual_path LIKE :path_filter
                ORDER BY fts.rank
                LIMIT :limit
            """)
            results = session.execute(
                sql, {"query": query, "limit": limit, "path_filter": f"{path_filter}%"}
            )
        else:
            sql = text("""
                SELECT
                    c.chunk_id,
                    c.path_id,
                    c.chunk_index,
                    c.chunk_text,
                    c.start_offset,
                    c.end_offset,
                    c.line_start,
                    c.line_end,
                    fp.virtual_path,
                    fts.rank as score
                FROM document_chunks_fts fts
                JOIN document_chunks c ON c.chunk_id = fts.chunk_id
                JOIN file_paths fp ON c.path_id = fp.path_id
                WHERE fts.chunk_text MATCH :query
                ORDER BY fts.rank
                LIMIT :limit
            """)
            results = session.execute(sql, {"query": query, "limit": limit})

        return [
            {
                "chunk_id": row.chunk_id,
                "path": row.virtual_path,
                "chunk_index": row.chunk_index,
                "chunk_text": row.chunk_text,
                "start_offset": row.start_offset,
                "end_offset": row.end_offset,
                "line_start": row.line_start,
                "line_end": row.line_end,
                "score": abs(float(row.score)),  # FTS5 rank is negative
            }
            for row in results
        ]

    def _postgres_keyword_search(
        self, session: Session, query: str, limit: int, path_filter: str | None
    ) -> list[dict[str, Any]]:
        """PostgreSQL keyword search using BM25 (pg_textsearch) or tsvector fallback.

        Uses pg_textsearch BM25 ranking when available (PostgreSQL 17+),
        falls back to ts_rank() on older versions or when extension unavailable.

        BM25 provides true relevance ranking with:
        - Inverse Document Frequency (IDF): Rare terms weighted higher
        - Term Frequency Saturation (k1=1.2): Prevents keyword stuffing
        - Length Normalization (b=0.75): Fair comparison across doc lengths

        Args:
            session: Database session
            query: Search query
            limit: Max results
            path_filter: Path filter

        Returns:
            Search results
        """
        if self.bm25_available:
            return self._postgres_bm25_search(session, query, limit, path_filter)
        return self._postgres_tsrank_search(session, query, limit, path_filter)

    def _postgres_bm25_search(
        self, session: Session, query: str, limit: int, path_filter: str | None
    ) -> list[dict[str, Any]]:
        """PostgreSQL keyword search using pg_textsearch BM25 ranking.

        BM25 scores are negative (lower = better match), so we ORDER BY ASC.
        We convert to positive scores (abs) for consistent API.

        Args:
            session: Database session
            query: Search query
            limit: Max results
            path_filter: Path filter

        Returns:
            Search results with BM25 relevance scores
        """
        if path_filter:
            sql = text("""
                SELECT
                    c.chunk_id,
                    c.path_id,
                    c.chunk_index,
                    c.chunk_text,
                    c.start_offset,
                    c.end_offset,
                    c.line_start,
                    c.line_end,
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
                SELECT
                    c.chunk_id,
                    c.path_id,
                    c.chunk_index,
                    c.chunk_text,
                    c.start_offset,
                    c.end_offset,
                    c.line_start,
                    c.line_end,
                    fp.virtual_path,
                    c.chunk_text <@> to_bm25query(:query, 'idx_chunks_bm25') as score
                FROM document_chunks c
                JOIN file_paths fp ON c.path_id = fp.path_id
                ORDER BY c.chunk_text <@> to_bm25query(:query, 'idx_chunks_bm25')
                LIMIT :limit
            """)
            results = session.execute(sql, {"query": query, "limit": limit})

        return [
            {
                "chunk_id": row.chunk_id,
                "path": row.virtual_path,
                "chunk_index": row.chunk_index,
                "chunk_text": row.chunk_text,
                "start_offset": row.start_offset,
                "end_offset": row.end_offset,
                "line_start": row.line_start,
                "line_end": row.line_end,
                "score": abs(float(row.score)),  # BM25 scores are negative
            }
            for row in results
        ]

    def _postgres_tsrank_search(
        self, session: Session, query: str, limit: int, path_filter: str | None
    ) -> list[dict[str, Any]]:
        """PostgreSQL keyword search using tsvector ts_rank() fallback.

        Used when pg_textsearch BM25 is not available.
        ts_rank() is not true BM25 and degrades at scale (25-30s on 800K rows).

        Args:
            session: Database session
            query: Search query
            limit: Max results
            path_filter: Path filter

        Returns:
            Search results with ts_rank scores
        """
        if path_filter:
            sql = text("""
                SELECT
                    c.chunk_id,
                    c.path_id,
                    c.chunk_index,
                    c.chunk_text,
                    c.start_offset,
                    c.end_offset,
                    c.line_start,
                    c.line_end,
                    fp.virtual_path,
                    ts_rank(to_tsvector('english', c.chunk_text), plainto_tsquery('english', :query)) as score
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
                SELECT
                    c.chunk_id,
                    c.path_id,
                    c.chunk_index,
                    c.chunk_text,
                    c.start_offset,
                    c.end_offset,
                    c.line_start,
                    c.line_end,
                    fp.virtual_path,
                    ts_rank(to_tsvector('english', c.chunk_text), plainto_tsquery('english', :query)) as score
                FROM document_chunks c
                JOIN file_paths fp ON c.path_id = fp.path_id
                WHERE to_tsvector('english', c.chunk_text) @@ plainto_tsquery('english', :query)
                ORDER BY score DESC
                LIMIT :limit
            """)
            results = session.execute(sql, {"query": query, "limit": limit})

        return [
            {
                "chunk_id": row.chunk_id,
                "path": row.virtual_path,
                "chunk_index": row.chunk_index,
                "chunk_text": row.chunk_text,
                "start_offset": row.start_offset,
                "end_offset": row.end_offset,
                "line_start": row.line_start,
                "line_end": row.line_end,
                "score": float(row.score),
            }
            for row in results
        ]

    def hybrid_search(
        self,
        session: Session,
        query: str,
        query_embedding: list[float],
        limit: int = 10,
        keyword_weight: float = 0.3,
        semantic_weight: float = 0.7,
        path_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid search combining keyword and semantic search.

        Args:
            session: Database session
            query: Text query for keyword search
            query_embedding: Embedding vector for semantic search
            limit: Maximum number of results
            keyword_weight: Weight for keyword search (default: 0.3)
            semantic_weight: Weight for semantic search (default: 0.7)
            path_filter: Optional path prefix filter

        Returns:
            List of search results ranked by combined score
        """
        # Get keyword results
        keyword_results = self.keyword_search(session, query, limit * 2, path_filter)

        # Get vector results
        vector_results = self.vector_search(session, query_embedding, limit * 2, path_filter)

        # Combine and re-rank
        results_map: dict[str, dict[str, Any]] = {}

        # Add keyword results
        for result in keyword_results:
            chunk_id = result["chunk_id"]
            results_map[chunk_id] = result.copy()
            results_map[chunk_id]["keyword_score"] = result["score"]
            results_map[chunk_id]["vector_score"] = 0.0

        # Add/merge vector results
        for result in vector_results:
            chunk_id = result["chunk_id"]
            if chunk_id in results_map:
                results_map[chunk_id]["vector_score"] = result["score"]
            else:
                results_map[chunk_id] = result.copy()
                results_map[chunk_id]["keyword_score"] = 0.0
                results_map[chunk_id]["vector_score"] = result["score"]

        # Calculate combined scores
        for result in results_map.values():
            result["score"] = (
                result["keyword_score"] * keyword_weight + result["vector_score"] * semantic_weight
            )

        # Sort by combined score and return top results
        ranked_results = sorted(results_map.values(), key=lambda x: x["score"], reverse=True)[
            :limit
        ]

        return ranked_results

    def get_stats(self) -> dict[str, Any]:
        """Get vector database statistics.

        Note: This method exists for backward compatibility with tests.
        New code should use SemanticSearch.get_index_stats() instead.
        """
        return {
            "vec_enabled": self.vec_available,
            "db_type": self.db_type,
        }

    def clear_index(self, session: Session) -> None:
        """Clear all search indexes.

        Note: This method exists for backward compatibility with tests.
        New code should use SemanticSearch.clear_index() instead.
        """
        from nexus.storage.models import DocumentChunkModel

        session.query(DocumentChunkModel).delete()
        session.commit()

    def delete_document(self, session: Session, path_id: str) -> None:
        """Delete document from index.

        Note: This method exists for backward compatibility with tests.
        New code should use SemanticSearch.delete_document_index() instead.

        Args:
            session: Database session
            path_id: Path ID of document to delete
        """
        from nexus.storage.models import DocumentChunkModel

        session.query(DocumentChunkModel).filter(DocumentChunkModel.path_id == path_id).delete()
        session.commit()
