"""Search module for Nexus.

Provides multiple search capabilities:
- Semantic search using vector embeddings (sqlite-vec, pgvector)
- Code search using Zoekt trigram indexing (optional)
- BM25S ranked text search (Issue #796)
- Hybrid search combining keyword and semantic search (Issue #798)
- Hot Search Daemon for sub-50ms response (Issue #951)
- Query Expansion for improved recall (Issue #1174)

Hybrid search fusion methods (Issue #798):
- RRF (Reciprocal Rank Fusion): Rank-based, no score normalization needed
- Weighted: Score-based with optional min-max normalization
- RRF Weighted: RRF with alpha weighting for BM25/vector bias

Query expansion (Issue #1174):
- LLM-based expansion generating lex/vec/hyde variants
- Smart triggering: skips expansion on strong BM25 signal
- Caching: reduces LLM API calls by 90%+
- OpenRouter support: DeepSeek, Gemini, GPT-4o-mini

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
from nexus.search.contextual_chunking import (
    ChunkContext,
    ContextGenerator,
    ContextualChunk,
    ContextualChunker,
    ContextualChunkingConfig,
    ContextualChunkResult,
    create_context_generator,
    create_heuristic_generator,
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
from nexus.search.graph_retrieval import (
    GraphContext,
    GraphEnhancedRetriever,
    GraphEnhancedSearchResult,
    GraphRetrievalConfig,
    graph_enhanced_fusion,
)
from nexus.search.hnsw_config import (
    DatasetScale,
    HNSWConfig,
    get_recommended_config,
    get_vector_count,
)
from nexus.search.manifest import SearchBrickManifest, verify_imports
from nexus.search.mobile_config import (
    EMBEDDING_MODELS,
    RERANKER_MODELS,
    TIER_PRESETS,
    DeviceTier,
    EmbeddingModelConfig,
    MobileSearchConfig,
    ModelProvider,
    RerankerModelConfig,
    SearchMode,
    auto_detect_config,
    create_custom_config,
    detect_device_tier,
    get_config_for_tier,
    list_available_models,
)
from nexus.search.mobile_providers import (
    CrossEncoderRerankerProvider,
    FastEmbedMobileProvider,
    GGUFEmbeddingProvider,
    MobileEmbeddingProvider,
    MobileRerankerProvider,
    MobileSearchService,
    Model2VecProvider,
    SentenceTransformersProvider,
    check_model_available,
    create_auto_service,
    create_mobile_embedding_provider,
    create_reranker_provider,
    create_service_from_config,
    download_gguf_model,
    download_model,
    download_models_for_tier,
)
from nexus.search.query_expansion import (
    CachedQueryExpander,
    ExpansionResult,
    ExpansionType,
    OpenRouterQueryExpander,
    QueryExpander,
    QueryExpansion,
    QueryExpansionConfig,
    QueryExpansionService,
    SignalDetector,
    create_cached_query_expander,
    create_query_expander,
    create_query_expansion_service,
    get_expansion_config_from_env,
)
from nexus.search.query_router import (
    QueryRouter,
    RoutedQuery,
    RoutingConfig,
    create_query_router,
)
from nexus.search.ranking import (
    AttributeWeights,
    RankingConfig,
    apply_attribute_boosting,
    detect_matched_field,
    get_ranking_config_from_env,
)
from nexus.search.results import BaseSearchResult
from nexus.search.semantic import SemanticSearch, SemanticSearchResult
from nexus.search.strategies import (
    GLOB_RUST_THRESHOLD,
    GREP_CACHED_TEXT_RATIO,
    GREP_PARALLEL_THRESHOLD,
    GREP_PARALLEL_WORKERS,
    GREP_SEQUENTIAL_THRESHOLD,
    GREP_ZOEKT_THRESHOLD,
    GlobStrategy,
    SearchStrategy,
)
from nexus.search.vector_db import VectorDatabase
from nexus.search.zoekt_client import (
    ZoektClient,
    ZoektMatch,
    get_zoekt_client,
    is_zoekt_available,
    zoekt_search,
)

__all__ = [
    # Search Brick (Issue #1520)
    "BaseSearchResult",
    "SearchBrickManifest",
    "verify_imports",
    # Strategy Enums (Issue #929, #1520)
    "SearchStrategy",
    "GlobStrategy",
    "GREP_SEQUENTIAL_THRESHOLD",
    "GREP_PARALLEL_THRESHOLD",
    "GREP_ZOEKT_THRESHOLD",
    "GREP_PARALLEL_WORKERS",
    "GREP_CACHED_TEXT_RATIO",
    "GLOB_RUST_THRESHOLD",
    # Chunking
    "ChunkStrategy",
    "DocumentChunk",
    "DocumentChunker",
    # Contextual Chunking (Issue #1192)
    "ChunkContext",
    "ContextGenerator",
    "ContextualChunk",
    "ContextualChunker",
    "ContextualChunkingConfig",
    "ContextualChunkResult",
    "create_context_generator",
    "create_heuristic_generator",
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
    # HNSW Configuration (Issue #1004)
    "HNSWConfig",
    "DatasetScale",
    "get_vector_count",
    "get_recommended_config",
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
    # Graph-Enhanced Retrieval (Issue #1040)
    "GraphEnhancedRetriever",
    "GraphRetrievalConfig",
    "GraphEnhancedSearchResult",
    "GraphContext",
    "graph_enhanced_fusion",
    # Query Expansion (Issue #1174)
    "QueryExpander",
    "QueryExpansion",
    "QueryExpansionConfig",
    "QueryExpansionService",
    "ExpansionType",
    "ExpansionResult",
    "OpenRouterQueryExpander",
    "CachedQueryExpander",
    "SignalDetector",
    "create_query_expander",
    "create_cached_query_expander",
    "create_query_expansion_service",
    "get_expansion_config_from_env",
    # Query Router (Issue #1041)
    "QueryRouter",
    "RoutedQuery",
    "RoutingConfig",
    "create_query_router",
    # Attribute Ranking (Issue #1092)
    "AttributeWeights",
    "RankingConfig",
    "apply_attribute_boosting",
    "detect_matched_field",
    "get_ranking_config_from_env",
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
    # Mobile/Edge Search Config (Issue #1213)
    "DeviceTier",
    "SearchMode",
    "ModelProvider",
    "EmbeddingModelConfig",
    "RerankerModelConfig",
    "MobileSearchConfig",
    "EMBEDDING_MODELS",
    "RERANKER_MODELS",
    "TIER_PRESETS",
    "detect_device_tier",
    "get_config_for_tier",
    "auto_detect_config",
    "create_custom_config",
    "list_available_models",
    # Mobile/Edge Search Providers (Issue #1213)
    "MobileEmbeddingProvider",
    "MobileRerankerProvider",
    "FastEmbedMobileProvider",
    "Model2VecProvider",
    "SentenceTransformersProvider",
    "GGUFEmbeddingProvider",
    "CrossEncoderRerankerProvider",
    "MobileSearchService",
    "create_mobile_embedding_provider",
    "create_reranker_provider",
    "create_service_from_config",
    "create_auto_service",
    "check_model_available",
    "download_model",
    "download_models_for_tier",
    # GGUF Model Download Helper (Issue #1214)
    "download_gguf_model",
]
