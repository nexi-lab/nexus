"""Workspace listing, Search clients and indexed query utilities.

Discovery and indexed queries use the Rust Search plugin on the owning Kernel.
"""

from nexus.bricks.search.config import SearchConfig, search_config_from_env
from nexus.bricks.search.query_router import (
    QueryRouter,
    RoutedQuery,
    RoutingConfig,
)
from nexus.bricks.search.results import BaseSearchResult
from nexus.bricks.search.search_service import SearchService
from nexus.bricks.search.zoekt_client import (
    ZoektClient,
    ZoektIndexManager,
    ZoektMatch,
)
from nexus.contracts.search_types import (
    AGGREGATION_WORDS,
    COMPARISON_WORDS,
    COMPLEX_PATTERNS,
    MULTIHOP_PATTERNS,
    TEMPORAL_WORDS,
)

__all__ = [
    "AGGREGATION_WORDS",
    "BaseSearchResult",
    "COMPARISON_WORDS",
    "COMPLEX_PATTERNS",
    "MULTIHOP_PATTERNS",
    "QueryRouter",
    "RoutedQuery",
    "RoutingConfig",
    "SearchConfig",
    "SearchService",
    "TEMPORAL_WORDS",
    "ZoektClient",
    "ZoektIndexManager",
    "ZoektMatch",
    "search_config_from_env",
]
