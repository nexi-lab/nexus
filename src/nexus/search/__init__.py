"""Search module for Nexus.

Provides multiple search capabilities:
- Semantic search using vector embeddings (sqlite-vec, pgvector)
- Code search using Zoekt trigram indexing (optional)
- BM25S ranked text search (Issue #796)
- Hybrid search combining keyword and semantic search (Issue #798)
- Hot Search Daemon for sub-50ms response (Issue #951)

Hybrid search fusion methods (Issue #798):
- RRF (Reciprocal Rank Fusion): Rank-based, no score normalization needed
- Weighted: Score-based with optional min-max normalization
- RRF Weighted: RRF with alpha weighting for BM25/vector bias

Embedding providers:
- OpenAI: High quality, recommended for production
- Voyage AI: Fast, cost-effective (voyage-3, voyage-3-lite)
- FastEmbed: Local ONNX embeddings (no API, free)

Embedding caching (Issue #950):
- CachedEmbeddingProvider: Wraps any provider with caching
- Reduces embedding API calls by 90% through content-hash deduplication
- Requires Redis/Dragonfly backend

Async support:
- AsyncSemanticSearch: Fully async for high-throughput scenarios
- Uses asyncpg/aiosqlite for non-blocking DB operations

Hot Search Daemon (Issue #951):
- SearchDaemon: Long-running service with pre-warmed indexes
- Sub-50ms query response with zero cold-start latency
- Integrates BM25S, pgvector, and Zoekt for multi-modal search
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
from nexus.search.daemon import (
    DaemonConfig,
    DaemonStats,
    SearchDaemon,
    SearchResult,
    create_and_start_daemon,
    get_search_daemon,
    set_search_daemon,
)
from nexus.search.embeddings import (
    CachedEmbeddingProvider,
    EmbeddingModel,
    EmbeddingProvider,
    FastEmbedProvider,
    OpenAIEmbeddingProvider,
    OpenRouterEmbeddingProvider,
    VoyageAIEmbeddingProvider,
    create_cached_embedding_provider,
    create_embedding_provider,
)
from nexus.search.fusion import (
    FusionConfig,
    FusionMethod,
    fuse_results,
    normalize_scores_minmax,
    rrf_fusion,
    rrf_weighted_fusion,
    weighted_fusion,
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
    "CachedEmbeddingProvider",
    "create_embedding_provider",
    "create_cached_embedding_provider",
    # Vector DB (sqlite-vec + pgvector)
    "VectorDatabase",
    # Semantic Search (sync)
    "SemanticSearch",
    "SemanticSearchResult",
    # Async Semantic Search (high-throughput)
    "AsyncSemanticSearch",
    "AsyncSearchResult",
    # Hybrid Search Fusion (Issue #798)
    "FusionConfig",
    "FusionMethod",
    "fuse_results",
    "normalize_scores_minmax",
    "rrf_fusion",
    "rrf_weighted_fusion",
    "weighted_fusion",
    # BM25S Fast Text Search (Issue #796)
    "BM25SIndex",
    "BM25SSearchResult",
    "CodeTokenizer",
    "get_bm25s_index",
    "is_bm25s_available",
    # Hot Search Daemon (Issue #951)
    "SearchDaemon",
    "DaemonConfig",
    "DaemonStats",
    "SearchResult",
    "create_and_start_daemon",
    "get_search_daemon",
    "set_search_daemon",
    # Zoekt Code Search
    "ZoektClient",
    "ZoektMatch",
    "get_zoekt_client",
    "is_zoekt_available",
    "zoekt_search",
]
