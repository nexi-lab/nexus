"""Search module for Nexus (Issue #2663 — txtai unified backend).

Provides:
- Hybrid BM25+dense search via txtai (pgvector backend)
- Semantic graph search via txtai
- Cross-encoder reranking (configurable)
- Zoekt trigram code search (optional, orthogonal to txtai)
- Hot Search Daemon for sub-50ms response
- Query expansion for improved recall
- Pluggable backend registry

Legacy modules (bm25s, vector_db*, fusion, ranking, graph_store,
graph_retrieval, mobile_*, embeddings, query_service) have been
replaced by txtai and deleted.
"""

from nexus.bricks.search.chunking import (
    ChunkStrategy,
    DocumentChunk,
    DocumentChunker,
)
from nexus.bricks.search.config import SearchConfig, search_config_from_env
from nexus.bricks.search.contextual_chunking import (
    ChunkContext,
    ContextGenerator,
    ContextualChunk,
    ContextualChunker,
    ContextualChunkingConfig,
    ContextualChunkResult,
    create_context_generator,
    create_heuristic_generator,
)
from nexus.bricks.search.daemon import (
    DaemonConfig,
    DaemonStats,
    SearchDaemon,
    SearchResult,
    create_and_start_daemon,
)
from nexus.bricks.search.indexing import IndexingPipeline, IndexProgress, IndexResult
from nexus.bricks.search.indexing_service import IndexingService
from nexus.bricks.search.manifest import SearchBrickManifest, verify_imports
from nexus.bricks.search.protocols import FileReaderProtocol
from nexus.bricks.search.query_expansion import (
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
from nexus.bricks.search.query_router import (
    QueryRouter,
    RoutedQuery,
    RoutingConfig,
)
from nexus.bricks.search.result_builders import build_result_from_row, build_semantic_result
from nexus.bricks.search.results import BaseSearchResult, detect_matched_field
from nexus.bricks.search.search_service import SearchService
from nexus.bricks.search.txtai_backend import (
    SEARCH_BACKENDS,
    SearchBackendProtocol,
    TxtaiBackend,
    create_backend,
)
from nexus.bricks.search.zoekt_client import (
    ZoektClient,
    ZoektIndexManager,
    ZoektMatch,
)
from nexus.contracts.search_types import (
    AGGREGATION_WORDS,
    COMPARISON_WORDS,
    COMPLEX_PATTERNS,
    GLOB_RUST_THRESHOLD,
    GREP_CACHED_TEXT_RATIO,
    GREP_PARALLEL_THRESHOLD,
    GREP_PARALLEL_WORKERS,
    GREP_SEQUENTIAL_THRESHOLD,
    GREP_ZOEKT_THRESHOLD,
    MULTIHOP_PATTERNS,
    TEMPORAL_WORDS,
    GlobStrategy,
    SearchStrategy,
)

__all__ = [
    # Search Brick (Issue #1520)
    "BaseSearchResult",
    "FileReaderProtocol",
    "SearchBrickManifest",
    "SearchConfig",
    "build_result_from_row",
    "build_semantic_result",
    "search_config_from_env",
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
    # Query Analysis Patterns (Issue #1499)
    "COMPARISON_WORDS",
    "TEMPORAL_WORDS",
    "AGGREGATION_WORDS",
    "MULTIHOP_PATTERNS",
    "COMPLEX_PATTERNS",
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
    # Indexing Pipeline (Issue #1094)
    "IndexingPipeline",
    "IndexResult",
    "IndexProgress",
    # CQRS Services (Issue #2075)
    "IndexingService",
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
    # Hot Search Daemon (Issue #951, #2663)
    "SearchDaemon",
    "DaemonConfig",
    "DaemonStats",
    "SearchResult",
    "create_and_start_daemon",
    # Zoekt Code Search
    "ZoektClient",
    "ZoektIndexManager",
    "ZoektMatch",
    # txtai backend (Issue #2663)
    "TxtaiBackend",
    "SearchBackendProtocol",
    "SEARCH_BACKENDS",
    "create_backend",
    "detect_matched_field",
    # Search Service (moved from services/search/, Issue #1287)
    "SearchService",
]
