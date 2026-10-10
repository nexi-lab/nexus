"""Workspace listing, Search clients and indexed query utilities.

Discovery and indexed queries use the Rust Search plugin on the owning Kernel.
"""

from nexus.bricks.search.results import BaseSearchResult
from nexus.bricks.search.search_service import SearchService

__all__ = [
    "BaseSearchResult",
    "SearchService",
]
