"""Workspace listing, Search clients and indexed query utilities.

Discovery and indexed queries use the Rust Search plugin on the owning Kernel.
"""

from nexus.bricks.search.config import SearchConfig, search_config_from_env
from nexus.bricks.search.results import BaseSearchResult
from nexus.bricks.search.search_service import SearchService
from nexus.bricks.search.zoekt_client import (
    ZoektClient,
    ZoektIndexManager,
    ZoektMatch,
)

__all__ = [
    "BaseSearchResult",
    "SearchConfig",
    "SearchService",
    "ZoektClient",
    "ZoektIndexManager",
    "ZoektMatch",
    "search_config_from_env",
]
