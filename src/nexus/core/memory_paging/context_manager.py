"""Main Context Manager - Primary memory tier (RAM equivalent).

Maintains a fixed-size FIFO buffer of active memories with automatic eviction
when capacity is exceeded. Uses hybrid LRU + importance scoring for eviction.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.storage.models import MemoryModel

logger = logging.getLogger(__name__)


@dataclass
class EvictionScore:
    """Score for eviction priority (lower = evict first)."""

    memory_id: str
    score: float
    recency_factor: float
    importance_factor: float


class ContextManager:
    """Manages main context (primary memory tier).

    Implements MemGPT's main context with:
    - Fixed capacity (token-based or count-based)
    - Automatic eviction when threshold exceeded
    - Hybrid LRU + importance scoring
    - FIFO ordering for sequential access

    Example:
        >>> ctx = ContextManager(max_items=100, eviction_threshold=0.7)
        >>> evicted = ctx.add(memory)
        >>> if evicted:
        ...     print(f"Evicted {len(evicted)} memories to recall")
    """

    def __init__(
        self,
        max_items: int = 100,
        eviction_threshold: float = 0.7,
        recency_weight: float = 0.6,
        importance_weight: float = 0.4,
    ):
        """Initialize context manager.

        Args:
            max_items: Maximum memories in main context
            eviction_threshold: Trigger eviction at this % of capacity (0-1)
            recency_weight: Weight for recency in eviction score (0-1)
            importance_weight: Weight for importance in eviction score (0-1)
        """
        self.max_items = max_items
        self.threshold = int(max_items * eviction_threshold)
        self.recency_weight = recency_weight
        self.importance_weight = importance_weight

        # FIFO buffer of memories
        self.buffer: deque[MemoryModel] = deque(maxlen=max_items)
        # Track last access time for LRU
        self.access_times: dict[str, datetime] = {}

    def add(self, memory: MemoryModel) -> list[MemoryModel]:
        """Add memory to main context, evicting if needed.

        Args:
            memory: Memory to add

        Returns:
            List of evicted memories (empty if no eviction)
        """
        evicted: list[MemoryModel] = []

        # Check if we need to evict
        if len(self.buffer) >= self.threshold:
            evicted = self._evict_to_recall()

        # Add new memory
        self.buffer.append(memory)
        self.access_times[memory.memory_id] = datetime.now(UTC)

        logger.debug(
            f"Added memory {memory.memory_id} to context "
            f"({len(self.buffer)}/{self.max_items}), evicted {len(evicted)}"
        )

        return evicted

    def get(self, memory_id: str) -> MemoryModel | None:
        """Get memory from context (updates LRU timestamp).

        Args:
            memory_id: Memory ID to retrieve

        Returns:
            Memory if found, None otherwise
        """
        for memory in self.buffer:
            if memory.memory_id == memory_id:
                # Update access time for LRU
                self.access_times[memory_id] = datetime.now(UTC)
                return memory
        return None

    def remove(self, memory_id: str) -> bool:
        """Remove memory from context.

        Args:
            memory_id: Memory ID to remove

        Returns:
            True if removed, False if not found
        """
        for i, memory in enumerate(self.buffer):
            if memory.memory_id == memory_id:
                del self.buffer[i]
                self.access_times.pop(memory_id, None)
                return True
        return False

    def get_all(self) -> list[MemoryModel]:
        """Get all memories in context (most recent first)."""
        return list(reversed(self.buffer))

    def count(self) -> int:
        """Get current number of memories in context."""
        return len(self.buffer)

    def is_full(self) -> bool:
        """Check if context is at capacity."""
        return len(self.buffer) >= self.max_items

    def clear(self) -> list[MemoryModel]:
        """Clear all memories from context.

        Returns:
            List of cleared memories
        """
        memories = list(self.buffer)
        self.buffer.clear()
        self.access_times.clear()
        return memories

    def _evict_to_recall(self, evict_count: int | None = None) -> list[MemoryModel]:
        """Evict memories using hybrid LRU + importance scoring.

        Args:
            evict_count: Number to evict (default: 20% of capacity)

        Returns:
            List of evicted memories
        """
        if evict_count is None:
            evict_count = max(1, int(self.max_items * 0.2))

        # Score all memories
        scores = self._compute_eviction_scores()

        # Sort by score (lowest first = evict first)
        scores.sort(key=lambda s: s.score)

        # Evict bottom N
        to_evict = scores[:evict_count]
        evicted: list[MemoryModel] = []

        for score in to_evict:
            for memory in list(self.buffer):
                if memory.memory_id == score.memory_id:
                    self.buffer.remove(memory)
                    self.access_times.pop(score.memory_id, None)
                    evicted.append(memory)
                    break

        logger.info(
            f"Evicted {len(evicted)} memories "
            f"(scores: {[s.score for s in to_evict[:3]]}...)"
        )

        return evicted

    def _compute_eviction_scores(self) -> list[EvictionScore]:
        """Compute eviction scores for all memories.

        Score = recency_weight * recency_factor + importance_weight * importance_factor
        Lower score = evict first
        """
        now = datetime.now(UTC)
        scores: list[EvictionScore] = []

        for memory in self.buffer:
            # Recency factor (0-1, higher = more recent)
            last_access = self.access_times.get(memory.memory_id, memory.created_at)
            if last_access and last_access.tzinfo is None:
                # Handle naive datetime
                last_access = last_access.replace(tzinfo=UTC)
            time_since_access = (now - last_access).total_seconds()
            max_age = 86400  # 24 hours
            recency_factor = max(0.0, 1.0 - (time_since_access / max_age))

            # Importance factor (0-1)
            importance_factor = memory.importance or 0.5

            # Combined score
            score = (
                self.recency_weight * recency_factor
                + self.importance_weight * importance_factor
            )

            scores.append(
                EvictionScore(
                    memory_id=memory.memory_id,
                    score=score,
                    recency_factor=recency_factor,
                    importance_factor=importance_factor,
                )
            )

        return scores
