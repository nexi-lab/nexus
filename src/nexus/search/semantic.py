"""Semantic search implementation for Nexus.

Provides semantic search capabilities using vector embeddings with:
- SQLite: sqlite-vec + FTS5
- PostgreSQL: pgvector + tsvector

Supports hybrid search combining keyword (FTS) and semantic (vector) search.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from nexus.search.chunking import ChunkStrategy, DocumentChunker
from nexus.search.embeddings import EmbeddingProvider
from nexus.search.vector_db import VectorDatabase
from nexus.storage.models import DocumentChunkModel, FilePathModel

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS


@dataclass
class SemanticSearchResult:
    """A semantic search result."""

    path: str
    chunk_index: int
    chunk_text: str
    score: float
    start_offset: int | None = None
    end_offset: int | None = None
    keyword_score: float | None = None
    vector_score: float | None = None


class SemanticSearch:
    """Semantic search engine for Nexus.

    Provides semantic and hybrid search using database-native extensions:
    - SQLite: sqlite-vec for vectors, FTS5 for keywords
    - PostgreSQL: pgvector for vectors, tsvector for keywords
    """

    def __init__(
        self,
        nx: NexusFS,
        embedding_provider: EmbeddingProvider | None = None,
        chunk_size: int = 1024,
        chunk_strategy: ChunkStrategy = ChunkStrategy.SEMANTIC,
    ):
        """Initialize semantic search.

        Args:
            nx: NexusFS instance
            embedding_provider: Embedding provider (optional - needed for semantic/hybrid search)
            chunk_size: Chunk size in tokens
            chunk_strategy: Chunking strategy
        """
        self.nx = nx
        self.chunk_size = chunk_size
        self.chunk_strategy = chunk_strategy

        # Initialize vector database with existing engine
        self.vector_db = VectorDatabase(self.nx.metadata.engine)

        # Initialize embedding provider (optional)
        # If None, only keyword search will be available
        self.embedding_provider: EmbeddingProvider | None = embedding_provider

        # Initialize chunker
        self.chunker = DocumentChunker(
            chunk_size=chunk_size, strategy=chunk_strategy, overlap_size=128
        )

    def initialize(self) -> None:
        """Initialize the search engine (create vector extensions and FTS tables)."""
        self.vector_db.initialize()

    async def index_document(self, path: str) -> int:
        """Index a document for semantic search.

        Uses cached/parsed text when available:
        - For connector files (GCS, S3, etc.): Uses content_cache.content_text
        - For local files: Uses file_metadata.parsed_text
        - Falls back to raw file content if no cached text available

        Args:
            path: Path to the document

        Returns:
            Number of chunks indexed

        Raises:
            NexusFileNotFoundError: If file doesn't exist
        """
        # Try to get searchable text from cache first (content_cache or file_metadata)
        content = self.nx.metadata.get_searchable_text(path)

        # Fall back to reading raw content if no cached text
        if content is None:
            content_raw = self.nx.read(path)
            if isinstance(content_raw, bytes):
                content = content_raw.decode("utf-8", errors="ignore")
            else:
                content = str(content_raw)  # Handle dict or other types

        # Get path_id from database
        with self.nx.metadata.SessionLocal() as session:
            stmt = select(FilePathModel).where(
                FilePathModel.virtual_path == path,
                FilePathModel.deleted_at.is_(None),
            )
            result = session.execute(stmt)
            file_model = result.scalar_one_or_none()

            if not file_model:
                raise ValueError(f"File not found in database: {path}")

            path_id = file_model.path_id

            # Delete existing chunks for this file
            chunk_stmt = select(DocumentChunkModel).where(DocumentChunkModel.path_id == path_id)
            chunk_result = session.execute(chunk_stmt)
            existing_chunks = chunk_result.scalars().all()

            for existing_chunk in existing_chunks:
                session.delete(existing_chunk)

            session.commit()

        # Chunk document
        chunks = self.chunker.chunk(content, path)

        if not chunks:
            return 0

        # Generate embeddings (if provider available)
        embeddings = None
        if self.embedding_provider:
            chunk_texts = [chunk.text for chunk in chunks]
            embeddings = await self.embedding_provider.embed_texts(chunk_texts)

        # Store chunks in database with optional embeddings
        with self.nx.metadata.SessionLocal() as session:
            chunk_ids = []
            for i, chunk in enumerate(chunks):
                # Create chunk model
                chunk_id = str(uuid.uuid4())
                chunk_ids.append(chunk_id)
                chunk_model = DocumentChunkModel(
                    chunk_id=chunk_id,
                    path_id=path_id,
                    chunk_index=i,
                    chunk_text=chunk.text,
                    chunk_tokens=chunk.tokens,
                    start_offset=chunk.start_offset,
                    end_offset=chunk.end_offset,
                    embedding_model=str(self.embedding_provider.__class__.__name__)
                    if self.embedding_provider
                    else None,
                    created_at=datetime.now(UTC),
                )
                session.add(chunk_model)

            session.commit()

            # Store embeddings if available AND vector extension is available
            if embeddings and self.vector_db.vec_available:
                for chunk_id, embedding in zip(chunk_ids, embeddings, strict=False):
                    self.vector_db.store_embedding(session, chunk_id, embedding)
                session.commit()

        return len(chunks)

    async def index_directory(self, path: str = "/") -> dict[str, int]:
        """Index all documents in a directory.

        Args:
            path: Root path to index (default: all files)

        Returns:
            Dictionary mapping file paths to number of chunks indexed
        """
        # List all files
        files = self.nx.list(path, recursive=True)

        # Filter to indexable files (exclude binary files, etc.)
        indexable_files = []
        for file in files:
            file_path = file if isinstance(file, str) else file.get("name", "")
            # Skip directories and non-text files
            if not file_path or file_path.endswith("/"):
                continue
            # Skip common binary extensions
            if file_path.endswith(
                (
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".gif",
                    ".pdf",
                    ".zip",
                    ".tar",
                    ".gz",
                    ".exe",
                    ".bin",
                )
            ):
                continue
            indexable_files.append(file_path)

        # Index each file
        results = {}
        for file_path in indexable_files:
            try:
                num_chunks = await self.index_document(file_path)
                results[file_path] = num_chunks
            except Exception as e:
                # Log error but continue
                import warnings

                warnings.warn(f"Failed to index {file_path}: {e}", stacklevel=2)
                results[file_path] = -1  # Indicate error

        return results

    async def search(
        self,
        query: str,
        path: str = "/",
        limit: int = 10,
        filters: dict[str, Any] | None = None,  # noqa: ARG002
        search_mode: str = "semantic",
    ) -> list[SemanticSearchResult]:
        """Search documents.

        Args:
            query: Natural language query
            path: Root path to search (default: all files)
            limit: Maximum number of results
            filters: Optional filters (currently unused)
            search_mode: Search mode - "semantic", "keyword", or "hybrid" (default: "semantic")

        Returns:
            List of search results ranked by relevance
        """
        # Build path filter
        path_filter = path if path != "/" else None

        with self.nx.metadata.SessionLocal() as session:
            if search_mode == "keyword":
                # Keyword-only search using FTS (no embeddings needed)
                results = self.vector_db.keyword_search(
                    session, query, limit=limit, path_filter=path_filter
                )
            elif search_mode == "hybrid":
                # Hybrid search (keyword + semantic) - requires embedding provider AND vector extension
                if not self.embedding_provider:
                    raise ValueError(
                        "Hybrid search requires an embedding provider. "
                        "Install with: pip install nexus-ai-fs[semantic-search-remote]"
                    )
                if not self.vector_db.vec_available:
                    raise ValueError(
                        "Hybrid search requires vector database extension. "
                        "Install sqlite-vec (https://github.com/asg017/sqlite-vec) "
                        "or pgvector (https://github.com/pgvector/pgvector). "
                        "Use search_mode='keyword' for FTS-only search."
                    )
                query_embedding = await self.embedding_provider.embed_text(query)
                results = self.vector_db.hybrid_search(
                    session,
                    query,
                    query_embedding,
                    limit=limit,
                    keyword_weight=0.3,
                    semantic_weight=0.7,
                    path_filter=path_filter,
                )
            else:
                # Semantic-only search (default) - requires embedding provider AND vector extension
                if not self.embedding_provider:
                    raise ValueError(
                        "Semantic search requires an embedding provider. "
                        "Install with: pip install nexus-ai-fs[semantic-search-remote] "
                        "Or use search_mode='keyword' for FTS-only search (no embeddings needed)"
                    )
                if not self.vector_db.vec_available:
                    raise ValueError(
                        "Semantic search requires vector database extension. "
                        "Install sqlite-vec (https://github.com/asg017/sqlite-vec) "
                        "or pgvector (https://github.com/pgvector/pgvector). "
                        "Use search_mode='keyword' for FTS-only search."
                    )
                query_embedding = await self.embedding_provider.embed_text(query)
                results = self.vector_db.vector_search(
                    session, query_embedding, limit=limit, path_filter=path_filter
                )

        # Convert to SemanticSearchResult
        search_results = []
        for result in results:
            search_results.append(
                SemanticSearchResult(
                    path=result["path"],
                    chunk_index=result["chunk_index"],
                    chunk_text=result["chunk_text"],
                    score=result["score"],
                    start_offset=result.get("start_offset"),
                    end_offset=result.get("end_offset"),
                    keyword_score=result.get("keyword_score"),
                    vector_score=result.get("vector_score"),
                )
            )

        return search_results

    async def delete_document_index(self, path: str) -> None:
        """Delete document index.

        Args:
            path: Path to the document
        """
        # Get path_id from database
        with self.nx.metadata.SessionLocal() as session:
            stmt = select(FilePathModel).where(
                FilePathModel.virtual_path == path,
                FilePathModel.deleted_at.is_(None),
            )
            result = session.execute(stmt)
            file_model = result.scalar_one_or_none()

            if not file_model:
                return  # File not found, nothing to delete

            path_id = file_model.path_id

            # Delete chunks from database (embeddings are in the same table)
            del_stmt = select(DocumentChunkModel).where(DocumentChunkModel.path_id == path_id)
            del_result = session.execute(del_stmt)
            chunks = del_result.scalars().all()

            for chunk in chunks:
                session.delete(chunk)

            session.commit()

    async def get_index_stats(self) -> dict[str, Any]:
        """Get indexing statistics.

        Returns:
            Dictionary with statistics
        """
        # Count total chunks
        with self.nx.metadata.SessionLocal() as session:
            chunk_stmt = select(DocumentChunkModel)
            chunk_result = session.execute(chunk_stmt)
            total_chunks = len(chunk_result.scalars().all())

            # Count indexed files
            path_stmt = select(DocumentChunkModel.path_id).distinct()
            path_result = session.execute(path_stmt)
            indexed_files = len(path_result.scalars().all())

        has_embeddings = self.embedding_provider is not None

        return {
            "total_chunks": total_chunks,
            "indexed_files": indexed_files,
            "embedding_model": str(self.embedding_provider.__class__.__name__)
            if self.embedding_provider
            else None,
            "chunk_size": self.chunk_size,
            "chunk_strategy": self.chunk_strategy.value,
            "database_type": self.vector_db.db_type,
            "search_capabilities": {
                "semantic": has_embeddings,
                "keyword": True,  # Always available via FTS
                "hybrid": has_embeddings,
            },
        }

    # Backward compatibility wrapper methods
    async def keyword_search(
        self, query: str, path: str = "/", limit: int = 10
    ) -> list[SemanticSearchResult]:
        """Keyword search (wrapper for search with mode='keyword')."""
        return await self.search(query, path=path, limit=limit, search_mode="keyword")

    async def semantic_search(
        self, query: str, path: str = "/", limit: int = 10
    ) -> list[SemanticSearchResult]:
        """Semantic search (wrapper for search with mode='semantic')."""
        return await self.search(query, path=path, limit=limit, search_mode="semantic")

    async def hybrid_search(
        self, query: str, path: str = "/", limit: int = 10
    ) -> list[SemanticSearchResult]:
        """Hybrid search (wrapper for search with mode='hybrid')."""
        return await self.search(query, path=path, limit=limit, search_mode="hybrid")

    async def get_stats(self) -> dict[str, Any]:
        """Get stats (wrapper for get_index_stats)."""
        return await self.get_index_stats()

    async def delete_document(self, path: str) -> None:
        """Delete document (wrapper for delete_document_index)."""
        return await self.delete_document_index(path)

    async def clear_index(self) -> None:
        """Clear the entire search index."""
        with self.nx.metadata.SessionLocal() as session:
            # Delete all chunks
            session.query(DocumentChunkModel).delete()
            session.commit()

    def close(self) -> None:
        """Close the search engine (no-op for now)."""
        pass
