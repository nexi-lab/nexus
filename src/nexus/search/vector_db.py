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

Backend-specific logic is delegated to:
- vector_db_sqlite.py: SQLite init, vector search, keyword search
- vector_db_postgres.py: PostgreSQL init, vector search, keyword search
"""

import asyncio
import atexit
import concurrent.futures
import logging
from typing import TYPE_CHECKING, Any

from nexus.search.hnsw_config import HNSWConfig

logger = logging.getLogger(__name__)

# Module-level shared thread pool for _run_sync (Issue #1520: replaces per-call creation)
_SYNC_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="nexus-vdb")
atexit.register(_SYNC_POOL.shutdown, wait=False)

def _run_sync(coro: Any) -> Any:
    """Run an async coroutine synchronously (Issue #1520: avoid core.sync_bridge import)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # If a loop is already running, use shared thread pool
    return _SYNC_POOL.submit(asyncio.run, coro).result()

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session

class VectorDatabase:
    """Vector database using sqlite-vec or pgvector based on database type.

    This class acts as a facade, delegating backend-specific operations
    to vector_db_sqlite and vector_db_postgres modules.
    """

    def __init__(self, engine: "Engine", hnsw_config: HNSWConfig | None = None):
        """Initialize vector database.

        Args:
            engine: SQLAlchemy engine
            hnsw_config: Optional HNSW configuration. If not provided, uses
                medium-scale defaults (m=24, ef_construction=128). Use
                HNSWConfig.for_dataset_size() for auto-configuration based
                on your dataset size.
        """
        self.engine = engine
        self.db_type = engine.dialect.name
        self.hnsw_config = hnsw_config or HNSWConfig.medium_scale()
        self._initialized = False
        self.vec_available = False  # Set to True if vector extension is loaded
        self.bm25_available = False  # Set to True if pg_textsearch BM25 is available
        self._sqlite_vec_loaded = False  # Track if we've set up the event listener

    def get_stats(self) -> dict[str, Any]:
        """Return statistics about the vector database."""
        return {
            "vec_enabled": self.vec_available,
            "bm25_enabled": self.bm25_available,
            "db_type": self.db_type,
            "initialized": self._initialized,
        }

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
        """Initialize SQLite with sqlite-vec and FTS5."""
        from nexus.search.vector_db_sqlite import init_sqlite, reload_sqlite_vec

        if not self._sqlite_vec_loaded:
            self.vec_available, self._sqlite_vec_loaded = init_sqlite(self.engine, conn)
        else:
            reload_sqlite_vec(conn)

    def _init_postgresql(self, conn: Any) -> None:
        """Initialize PostgreSQL with pgvector and pg_textsearch."""
        from nexus.search.vector_db_postgres import init_postgresql

        self.vec_available, self.bm25_available = init_postgresql(conn, self.hnsw_config)

    def store_embedding(self, session: "Session", chunk_id: str, embedding: list[float]) -> None:
        """Store embedding for a chunk.

        Args:
            session: Database session
            chunk_id: Chunk ID
            embedding: Embedding vector
        """
        if self.db_type == "sqlite":
            from nexus.search.vector_db_sqlite import sqlite_store_embedding

            sqlite_store_embedding(session, chunk_id, embedding)
        elif self.db_type == "postgresql":
            from nexus.search.vector_db_postgres import postgres_store_embedding

            postgres_store_embedding(session, chunk_id, embedding)

    def vector_search(
        self,
        session: "Session",
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
            from nexus.search.vector_db_sqlite import sqlite_vector_search

            return sqlite_vector_search(session, query_embedding, limit, path_filter)
        elif self.db_type == "postgresql":
            from nexus.search.vector_db_postgres import postgres_vector_search

            return postgres_vector_search(
                session, query_embedding, limit, path_filter, self.hnsw_config
            )
        else:
            raise ValueError(f"Unsupported database type: {self.db_type}")

    def keyword_search(
        self, session: "Session", query: str, limit: int = 10, path_filter: str | None = None
    ) -> list[dict[str, Any]]:
        """Search by keywords using Zoekt, BM25S, or FTS.

        Search priority:
        1. Zoekt (fast trigram-based code search)
        2. BM25S (fast in-memory BM25 with code-aware tokenization, Issue #796)
        3. pg_textsearch BM25 (PostgreSQL 17+)
        4. FTS5 (SQLite)

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

        # Try BM25S for fast ranked text search (Issue #796)
        bm25s_results = self._try_keyword_search_with_bm25s(query, limit, path_filter)
        if bm25s_results is not None:
            logger.debug(f"[KEYWORD] BM25S returned {len(bm25s_results)} results")
            return bm25s_results

        # Fall back to FTS
        logger.debug("[KEYWORD] Using FTS fallback")
        if self.db_type == "sqlite":
            from nexus.search.vector_db_sqlite import sqlite_keyword_search

            return sqlite_keyword_search(session, query, limit, path_filter)
        elif self.db_type == "postgresql":
            from nexus.search.vector_db_postgres import postgres_keyword_search

            return postgres_keyword_search(session, query, limit, path_filter, self.bm25_available)
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

        # Check if Zoekt is available (sync wrapper, Issue #1520)
        is_available = _run_sync(client.is_available())

        if not is_available:
            return None

        logger.debug("[KEYWORD] Using Zoekt for accelerated search")

        try:
            # Build Zoekt query
            zoekt_query = query
            if path_filter:
                zoekt_query = f"file:{path_filter.lstrip('/')} {zoekt_query}"

            # Run search
            matches = _run_sync(client.search(zoekt_query, num=limit * 2))

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

    def _try_keyword_search_with_bm25s(
        self, query: str, limit: int, path_filter: str | None
    ) -> list[dict[str, Any]] | None:
        """Try to use BM25S for keyword search (Issue #796).

        BM25S provides fast ranked text search with:
        - Code-aware tokenization (camelCase, snake_case splitting)
        - In-memory sparse matrix scoring (500x faster than rank-bm25)
        - True BM25 with IDF weighting

        Args:
            query: Search query
            limit: Maximum results
            path_filter: Optional path prefix

        Returns:
            List of results if BM25S succeeded, None to fall back to FTS
        """
        try:
            from nexus.search.bm25s_search import BM25SIndex, is_bm25s_available
        except ImportError:
            return None

        if not is_bm25s_available():
            return None

        index = BM25SIndex.get_instance()

        # Check if index is initialized and has documents (Issue #1520)
        if not _run_sync(index.initialize()):
            return None

        stats = _run_sync(index.get_stats())
        if stats.get("total_documents", 0) == 0:
            return None

        logger.debug("[KEYWORD] Using BM25S for fast ranked text search")

        try:
            # Run search
            bm25s_results = _run_sync(
                index.search(query=query, limit=limit, path_filter=path_filter)
            )

            if not bm25s_results:
                return None

            # Convert BM25S results to keyword_search format
            results = []
            for r in bm25s_results:
                results.append(
                    {
                        "chunk_id": None,
                        "path_id": r.path_id,
                        "chunk_index": 0,
                        "chunk_text": r.content_preview,
                        "start_offset": 0,
                        "end_offset": len(r.content_preview),
                        "line_start": 1,
                        "line_end": None,
                        "virtual_path": r.path,
                        "score": r.score,
                    }
                )

            logger.debug(f"[KEYWORD] BM25S: {len(results)} results")
            return results

        except Exception as e:
            logger.warning(f"[KEYWORD] BM25S search failed, falling back to FTS: {e}")
            return None

    def hybrid_search(
        self,
        session: "Session",
        query: str,
        query_embedding: list[float],
        limit: int = 10,
        alpha: float = 0.5,
        fusion_method: str = "rrf",
        rrf_k: int = 60,
        normalize_scores: bool = True,
        path_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid search combining keyword and semantic search.

        Combines BM25/keyword search with vector/semantic search using
        configurable fusion algorithms. Default is RRF (Reciprocal Rank Fusion).

        Args:
            session: Database session
            query: Text query for keyword search
            query_embedding: Embedding vector for semantic search
            limit: Maximum number of results
            alpha: Weight for vector search (0.0 = all BM25, 1.0 = all vector).
                   Used by 'weighted' and 'rrf_weighted' fusion methods.
            fusion_method: Fusion algorithm - "rrf" (default), "weighted", or "rrf_weighted"
            rrf_k: RRF constant (default: 60, per original paper)
            normalize_scores: Apply min-max normalization for weighted fusion
            path_filter: Optional path prefix filter

        Returns:
            List of search results ranked by combined score
        """
        from nexus.search.fusion import FusionConfig, FusionMethod, fuse_results

        # Get keyword results (retrieve more for better fusion)
        keyword_results = self.keyword_search(session, query, limit * 3, path_filter)

        # Get vector results
        vector_results = self.vector_search(session, query_embedding, limit * 3, path_filter)

        # Create fusion config
        config = FusionConfig(
            method=FusionMethod(fusion_method),
            alpha=alpha,
            rrf_k=rrf_k,
            normalize_scores=normalize_scores,
        )

        # Fuse results using shared algorithm
        return fuse_results(
            keyword_results,
            vector_results,
            config=config,
            limit=limit,
            id_key="chunk_id",
        )
