"""Search module for Nexus.

Kernel-tier surfaces (``SearchService`` for grep/glob + federation
dispatch) live here alongside the ``QueryRouter`` / ``QueryExpansion``
helpers and the ``PgFtsBackend`` / ``SqliteFtsBackend`` / ``Zoekt``
backends that the router and CLI still use.

Semantic + hybrid ``/query`` are served by the Rust
``nexus-search-plugin`` cdylib; boot wiring lives in
``nexus.server.lifespan.search`` and the Python-side proxy is
``nexus.bricks.search.rust_daemon.RustSearchDaemon``.
"""

from nexus.bricks.search.config import SearchConfig, search_config_from_env
from nexus.bricks.search.pg_fts_backend import PgFtsBackend
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
from nexus.bricks.search.sqlite_fts_backend import SqliteFtsBackend
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
    "AGGREGATION_WORDS",
    "BaseSearchResult",
    "COMPARISON_WORDS",
    "COMPLEX_PATTERNS",
    "CachedQueryExpander",
    "ExpansionResult",
    "ExpansionType",
    "GLOB_RUST_THRESHOLD",
    "GREP_CACHED_TEXT_RATIO",
    "GREP_PARALLEL_THRESHOLD",
    "GREP_PARALLEL_WORKERS",
    "GREP_SEQUENTIAL_THRESHOLD",
    "GREP_ZOEKT_THRESHOLD",
    "GlobStrategy",
    "MULTIHOP_PATTERNS",
    "OpenRouterQueryExpander",
    "PgFtsBackend",
    "QueryExpander",
    "QueryExpansion",
    "QueryExpansionConfig",
    "QueryExpansionService",
    "QueryRouter",
    "RoutedQuery",
    "RoutingConfig",
    "SearchConfig",
    "SearchService",
    "SearchStrategy",
    "SignalDetector",
    "SqliteFtsBackend",
    "TEMPORAL_WORDS",
    "ZoektClient",
    "ZoektIndexManager",
    "ZoektMatch",
    "build_result_from_row",
    "build_semantic_result",
    "create_cached_query_expander",
    "create_query_expander",
    "create_query_expansion_service",
    "detect_matched_field",
    "get_expansion_config_from_env",
    "search_config_from_env",
]
