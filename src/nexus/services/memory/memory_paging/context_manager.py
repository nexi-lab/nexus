"""Main Context Manager - Primary memory tier (RAM equivalent).

Maintains a fixed-size FIFO buffer of active memories with automatic eviction
when capacity is exceeded. Uses hybrid LRU + importance scoring for eviction.

Thread-safe: All public methods are guarded by a threading.Lock.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm.exc import DetachedInstanceError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.storage.models import MemoryModel

logger = logging.getLogger(__name__)

# Maximum allowed main context capacity to prevent O(n log n) eviction at scale
MAX_CAPACITY = 1000

# Eviction ratio: fraction of capacity evicted when threshold is exceeded
EVICTION_RATIO = 0.2

# Max age in seconds for recency scoring (24 hours)
MAX_AGE_SECONDS = 86400


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
    - Thread-safe: all operations protected by lock

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
        if max_items <= 0:
            raise ValueError(
                f"max_items must be > 0, got {max_items}. "
                f"Use a positive integer up to {MAX_CAPACITY}."
            )
        if max_items > MAX_CAPACITY:
            raise ValueError(
                f"max_items must be <= {MAX_CAPACITY}, got {max_items}. "
                f"Increase MAX_CAPACITY if higher capacity is needed."
            )
        if not 0.0 < eviction_threshold <= 1.0:
            raise ValueError(f"eviction_threshold must be in (0, 1], got {eviction_threshold}.")
        if not 0.0 <= recency_weight <= 1.0:
            raise ValueError(f"recency_weight must be in [0, 1], got {recency_weight}.")
        if not 0.0 <= importance_weight <= 1.0:
            raise ValueError(f"importance_weight must be in [0, 1], got {importance_weight}.")

        self.max_items = max_items
        self.threshold = int(max_items * eviction_threshold)
        self.recency_weight = recency_weight
        self.importance_weight = importance_weight

        # Thread lock for all mutable state
        self._lock = threading.Lock()

        # FIFO buffer of memories (no maxlen - we manage size via eviction)
        self._buffer: deque[MemoryModel] = deque()
        # Parallel ordered list of memory IDs (avoids accessing ORM attrs on
        # potentially detached objects during eviction scoring)
        self._buffer_ids: deque[str] = deque()
        # O(1) lookup index: memory_id -> MemoryModel
        self._index: dict[str, MemoryModel] = {}
        # Track last access time for LRU
        self._access_times: dict[str, datetime] = {}
        # Cache importance scores to avoid detached instance access
        self._importance_cache: dict[str, float] = {}

    @property
    def buffer(self) -> list[MemoryModel]:
        """Thread-safe snapshot of the buffer (returns a copy)."""
        with self._lock:
            return list(self._buffer)

    @property
    def access_times(self) -> dict[str, datetime]:
        """Thread-safe snapshot of access times (returns a copy)."""
        with self._lock:
            return dict(self._access_times)

    def add(self, memory: MemoryModel) -> list[MemoryModel]:
        """Add memory to main context, evicting if needed.

        Args:
            memory: Memory to add

        Returns:
            List of evicted memories (empty if no eviction)
        """
        with self._lock:
            evicted: list[MemoryModel] = []

            # Cache memory_id eagerly (before any session ops may detach it)
            mid = memory.memory_id

            # Check if we need to evict
            if len(self._buffer) >= self.threshold:
                evicted = self._evict_to_recall_locked()

            # Add new memory
            self._buffer.append(memory)
            self._buffer_ids.append(mid)
            self._index[mid] = memory
            self._access_times[mid] = datetime.now(UTC)
            # Cache importance to avoid detached instance access later
            try:
                self._importance_cache[mid] = memory.importance or 0.5
            except DetachedInstanceError:
                logger.debug(f"Detached instance for {mid}, using default importance 0.5")
                self._importance_cache[mid] = 0.5

            # Enforce max capacity (drop oldest if over limit)
            while len(self._buffer) > self.max_items:
                self._buffer.popleft()
                dropped_id = self._buffer_ids.popleft()
                self._index.pop(dropped_id, None)
                self._access_times.pop(dropped_id, None)
                self._importance_cache.pop(dropped_id, None)

            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    f"Added memory {mid} to context "
                    f"({len(self._buffer)}/{self.max_items}), evicted {len(evicted)}"
                )

            return evicted

    def get(self, memory_id: str) -> MemoryModel | None:
        """Get memory from context (updates LRU timestamp).

        Args:
            memory_id: Memory ID to retrieve

        Returns:
            Memory if found, None otherwise
        """
        with self._lock:
            memory = self._index.get(memory_id)
            if memory is not None:
                self._access_times[memory_id] = datetime.now(UTC)
            return memory

    def remove(self, memory_id: str) -> bool:
        """Remove memory from context.

        Args:
            memory_id: Memory ID to remove

        Returns:
            True if removed, False if not found
        """
        with self._lock:
            if memory_id not in self._index:
                return False
            self._index.pop(memory_id)
            self._access_times.pop(memory_id, None)
            self._importance_cache.pop(memory_id, None)
            # Rebuild buffer without the removed ID - O(n) but unavoidable for deque
            new_buffer: deque[MemoryModel] = deque()
            new_ids: deque[str] = deque()
            for mem, mid in zip(self._buffer, self._buffer_ids, strict=True):
                if mid != memory_id:
                    new_buffer.append(mem)
                    new_ids.append(mid)
            self._buffer = new_buffer
            self._buffer_ids = new_ids
            return True

    def get_all(self) -> list[MemoryModel]:
        """Get all memories in context (most recent first)."""
        with self._lock:
            return list(reversed(self._buffer))

    def count(self) -> int:
        """Get current number of memories in context."""
        with self._lock:
            return len(self._buffer)

    def is_full(self) -> bool:
        """Check if context is at capacity."""
        with self._lock:
            return len(self._buffer) >= self.max_items

    def clear(self) -> list[MemoryModel]:
        """Clear all memories from context.

        Returns:
            List of cleared memories
        """
        with self._lock:
            memories = list(self._buffer)
            self._buffer.clear()
            self._buffer_ids.clear()
            self._index.clear()
            self._access_times.clear()
            self._importance_cache.clear()
            return memories

    def warm_up(self, session: Session, zone_id: str, limit: int | None = None) -> int:
        """Load recent memories from DB into the in-memory FIFO buffer.

        Called during initialization to restore context from persisted state.
        Uses a single lock acquisition for the entire batch (not per-memory).
        Only loads main-tier memories (excludes recall/archival namespaces).

        Args:
            session: SQLAlchemy session for querying MemoryModel.
            zone_id: Zone ID to filter memories.
            limit: Max memories to load (defaults to self.max_items).

        Returns:
            Number of memories loaded into the buffer.
        """
        from sqlalchemy import select

        from nexus.storage.models import MemoryModel

        load_limit = limit or self.max_items

        stmt = (
            select(MemoryModel)
            .where(
                MemoryModel.zone_id == zone_id,
                MemoryModel.state == "active",
            )
            # Exclude recall/archival tier memories (NULL namespace is allowed)
            .where(
                (MemoryModel.namespace.is_(None))
                | (
                    ~MemoryModel.namespace.like("recall%")
                    & ~MemoryModel.namespace.like("archival%")
                )
            )
            .order_by(MemoryModel.created_at.asc())
            .limit(load_limit)
        )
        memories = list(session.execute(stmt).scalars().all())

        # Batch-add under a single lock acquisition (avoids N lock round-trips)
        loaded_count = self._add_batch(memories)

        logger.info(
            f"Warm-up loaded {loaded_count} memories into context "
            f"(zone={zone_id}, capacity={self.max_items})"
        )
        return loaded_count

    def _add_batch(self, memories: list[MemoryModel]) -> int:
        """Add multiple memories under a single lock acquisition.

        Does not trigger eviction -- intended for warm-up where memories
        are already persisted.  Truncates to max_items (oldest-first order
        assumed so newest end up at the tail).

        Args:
            memories: Memories to load into the buffer.

        Returns:
            Number of memories actually loaded.
        """
        if not memories:
            return 0

        with self._lock:
            for memory in memories:
                mid = memory.memory_id
                self._buffer.append(memory)
                self._buffer_ids.append(mid)
                self._index[mid] = memory
                self._access_times[mid] = datetime.now(UTC)
                try:
                    self._importance_cache[mid] = memory.importance or 0.5
                except DetachedInstanceError:
                    logger.debug(f"Detached instance for {mid}, using default importance 0.5")
                    self._importance_cache[mid] = 0.5

            # Enforce max capacity (drop oldest if over limit)
            while len(self._buffer) > self.max_items:
                self._buffer.popleft()
                dropped_id = self._buffer_ids.popleft()
                self._index.pop(dropped_id, None)
                self._access_times.pop(dropped_id, None)
                self._importance_cache.pop(dropped_id, None)

            loaded = len(self._buffer)

        return loaded

    def _evict_to_recall_locked(self, evict_count: int | None = None) -> list[MemoryModel]:
        """Evict memories using hybrid LRU + importance scoring.

        MUST be called with self._lock held.

        Args:
            evict_count: Number to evict (default: 20% of capacity)

        Returns:
            List of evicted memories
        """
        if evict_count is None:
            evict_count = max(1, int(self.max_items * EVICTION_RATIO))

        # Score all memories
        scores = self._compute_eviction_scores()

        # Sort by score (lowest first = evict first)
        scores.sort(key=lambda s: s.score)

        # Collect IDs to evict
        evict_ids = {s.memory_id for s in scores[:evict_count]}

        # Single-pass rebuild: separate evicted from kept (using cached IDs)
        evicted: list[MemoryModel] = []
        kept: deque[MemoryModel] = deque()
        kept_ids: deque[str] = deque()

        for memory, mid in zip(self._buffer, self._buffer_ids, strict=True):
            if mid in evict_ids:
                evicted.append(memory)
                self._index.pop(mid, None)
                self._access_times.pop(mid, None)
                self._importance_cache.pop(mid, None)
            else:
                kept.append(memory)
                kept_ids.append(mid)

        self._buffer = kept
        self._buffer_ids = kept_ids

        if logger.isEnabledFor(logging.INFO):
            logger.info(
                f"Evicted {len(evicted)} memories (scores: {[s.score for s in scores[:3]]}...)"
            )

        return evicted

    def _compute_eviction_scores(self) -> list[EvictionScore]:
        """Compute eviction scores for all memories.

        Score = recency_weight * recency_factor + importance_weight * importance_factor
        Lower score = evict first

        Uses only cached IDs/values -- never accesses ORM attributes on buffer
        objects, which may be detached from their SQLAlchemy session.

        MUST be called with self._lock held.
        """
        now = datetime.now(UTC)
        scores: list[EvictionScore] = []

        for memory_id in self._buffer_ids:
            # Recency factor (0-1, higher = more recent)
            # access_times is always populated by add(), so this should never be None
            last_access = self._access_times.get(memory_id, now)
            if last_access.tzinfo is None:
                last_access = last_access.replace(tzinfo=UTC)
            time_since_access = (now - last_access).total_seconds()
            recency_factor = max(0.0, 1.0 - (time_since_access / MAX_AGE_SECONDS))

            # Importance factor (0-1) - use cached value
            importance_factor = self._importance_cache.get(memory_id, 0.5)

            # Combined score
            score = (
                self.recency_weight * recency_factor + self.importance_weight * importance_factor
            )

            scores.append(
                EvictionScore(
                    memory_id=memory_id,
                    score=score,
                    recency_factor=recency_factor,
                    importance_factor=importance_factor,
                )
            )

        return scores
