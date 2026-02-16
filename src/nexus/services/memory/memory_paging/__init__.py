"""MemGPT-style 3-tier memory paging system (Issue #1258).

Implements hierarchical memory management with three tiers:
- Main Context (RAM): Fixed-size FIFO buffer with LRU eviction
- Recall Storage: Sequential/temporal access for recent history
- Archival Storage: Semantic search for long-term knowledge

Reference: https://arxiv.org/abs/2310.08560 (MemGPT paper)
"""

from nexus.services.memory.memory_paging.archival_store import ArchivalStore
from nexus.services.memory.memory_paging.context_manager import ContextManager
from nexus.services.memory.memory_paging.namespace_util import strip_tier_prefix
from nexus.services.memory.memory_paging.pager import MemoryPager
from nexus.services.memory.memory_paging.recall_store import RecallStore

__all__ = [
    "ContextManager",
    "RecallStore",
    "ArchivalStore",
    "MemoryPager",
    "strip_tier_prefix",
]
