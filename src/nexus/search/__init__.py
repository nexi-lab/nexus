"""Semantic search module for Nexus.

Provides semantic search capabilities using vector embeddings with:
- SQLite: sqlite-vec + FTS5
- PostgreSQL: pgvector + tsvector

Supports hybrid search combining keyword and semantic search.
"""

from nexus.search.chunking import ChunkStrategy, DocumentChunk, DocumentChunker
from nexus.search.embeddings import (
    EmbeddingModel,
    EmbeddingProvider,
    OpenAIEmbeddingProvider,
    VoyageAIEmbeddingProvider,
    create_embedding_provider,
)
from nexus.search.semantic import SemanticSearch, SemanticSearchResult
from nexus.search.vector_db import VectorDatabase

__all__ = [
    # Chunking
    "ChunkStrategy",
    "DocumentChunk",
    "DocumentChunker",
    # Embeddings
    "EmbeddingModel",
    "EmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "VoyageAIEmbeddingProvider",
    "create_embedding_provider",
    # Vector DB (sqlite-vec + pgvector)
    "VectorDatabase",
    # Semantic Search
    "SemanticSearch",
    "SemanticSearchResult",
]
