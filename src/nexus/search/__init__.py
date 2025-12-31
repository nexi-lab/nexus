"""Search module for Nexus.

Provides multiple search capabilities:
- Semantic search using vector embeddings (sqlite-vec, pgvector)
- Code search using Zoekt trigram indexing (optional)
- BM25S ranked text search (Issue #796)
- Hybrid search combining keyword and semantic search

Embedding providers:
- OpenAI: High quality, recommended for production
- Voyage AI: Fast, cost-effective (voyage-3, voyage-3-lite)
- FastEmbed: Local ONNX embeddings (no API, free)

Async support:
- AsyncSemanticSearch: Fully async for high-throughput scenarios
- Uses asyncpg/aiosqlite for non-blocking DB operations
"""

from nexus.search.async_search import AsyncSearchResult, AsyncSemanticSearch
from nexus.search.bm25s_search import (
    BM25SIndex,
    BM25SSearchResult,
    CodeTokenizer,
    get_bm25s_index,
    is_bm25s_available,
)
from nexus.search.chunking import (
    ChunkStrategy,
    DocumentChunk,
    DocumentChunker,
)
from nexus.search.embeddings import (
    EmbeddingModel,
    EmbeddingProvider,
    FastEmbedProvider,
    OpenAIEmbeddingProvider,
    OpenRouterEmbeddingProvider,
    VoyageAIEmbeddingProvider,
    create_embedding_provider,
)
from nexus.search.semantic import SemanticSearch, SemanticSearchResult
from nexus.search.vector_db import VectorDatabase
from nexus.search.zoekt_client import (
    ZoektClient,
    ZoektMatch,
    get_zoekt_client,
    is_zoekt_available,
    zoekt_search,
)

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
    "OpenRouterEmbeddingProvider",
    "FastEmbedProvider",
    "create_embedding_provider",
    # Vector DB (sqlite-vec + pgvector)
    "VectorDatabase",
    # Semantic Search (sync)
    "SemanticSearch",
    "SemanticSearchResult",
    # Async Semantic Search (high-throughput)
    "AsyncSemanticSearch",
    "AsyncSearchResult",
    # BM25S Fast Text Search (Issue #796)
    "BM25SIndex",
    "BM25SSearchResult",
    "CodeTokenizer",
    "get_bm25s_index",
    "is_bm25s_available",
    # Zoekt Code Search
    "ZoektClient",
    "ZoektMatch",
    "get_zoekt_client",
    "is_zoekt_available",
    "zoekt_search",
]
