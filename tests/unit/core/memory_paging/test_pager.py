"""Tests for MemoryPager (Issue #1258)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.services.memory.memory_paging import MemoryPager
from nexus.services.memory.memory_paging.context_manager import MAX_CAPACITY, ContextManager
from nexus.storage.models import Base, MemoryModel


@pytest.fixture
def engine():
    """Create in-memory test database engine."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    """Create session factory bound to engine."""
    return sessionmaker(bind=engine)


@pytest.fixture
def session(session_factory):
    """Create a session for direct DB operations in tests."""
    sess = session_factory()
    yield sess
    sess.close()


@pytest.fixture
def pager(session_factory):
    """Create MemoryPager instance."""
    return MemoryPager(
        session_factory=session_factory,
        zone_id="test",
        main_capacity=5,  # Small capacity for testing
        recall_max_age_hours=24.0,
        warm_up=False,  # Don't warm up in unit tests
    )


def create_test_memory(content: str = "test", importance: float = 0.5) -> MemoryModel:
    """Create a test memory."""
    return MemoryModel(
        content_hash=f"hash_{content}",
        zone_id="test",
        user_id="alice",
        agent_id="agent1",
        importance=importance,
    )


class TestMemoryPager:
    """Test MemoryPager orchestration."""

    def test_add_to_main_below_capacity(self, pager):
        """Adding below capacity should not trigger eviction."""
        memory = create_test_memory("mem1")

        pager.add_to_main(memory)

        assert pager.context.count() == 1
        assert pager.recall.count() == 0
        assert pager.archival.count() == 0

    def test_add_to_main_exceeds_capacity(self, pager):
        """Exceeding capacity should evict to recall."""
        # Add 6 memories (capacity is 5)
        for i in range(6):
            memory = create_test_memory(f"mem{i}")
            pager.add_to_main(memory)

        stats = pager.get_stats()

        # Main should be at/near capacity
        assert stats["main"]["count"] <= 5
        # At least one should have been evicted to recall
        assert stats["recall"]["count"] > 0

    def test_get_from_main(self, pager):
        """Should retrieve from main context."""
        memory = create_test_memory("mem1")
        pager.add_to_main(memory)

        retrieved = pager.get_from_main(memory.memory_id)

        assert retrieved is not None
        assert retrieved.memory_id == memory.memory_id

    def test_get_recent_context(self, pager):
        """Should combine main + recall for recent context."""
        # Add 7 memories (exceeds capacity of 5)
        for i in range(7):
            memory = create_test_memory(f"mem{i}")
            pager.add_to_main(memory)

        recent = pager.get_recent_context(limit=6)

        # Should get memories from both main and recall
        assert len(recent) <= 6
        # Should have at least the main context memories
        assert len(recent) >= pager.context.count()

    def test_get_stats(self, pager):
        """Should return correct statistics."""
        # Add 3 memories
        for i in range(3):
            memory = create_test_memory(f"mem{i}")
            pager.add_to_main(memory)

        stats = pager.get_stats()

        assert "total_memories" in stats
        assert "main" in stats
        assert "recall" in stats
        assert "archival" in stats
        assert stats["main"]["count"] == 3
        assert stats["main"]["capacity"] == 5
        assert 0 <= stats["main"]["utilization"] <= 1.0

    def test_cascading_eviction(self, pager):
        """Multiple adds should cascade through tiers."""
        # Add many memories
        for i in range(10):
            memory = create_test_memory(f"mem{i}", importance=0.5)
            pager.add_to_main(memory)

        stats = pager.get_stats()

        # Should have distributed across tiers
        assert stats["total_memories"] == 10
        # Main should be at capacity
        assert stats["main"]["count"] <= 5


class TestContextManagerValidation:
    """Test capacity validation in ContextManager."""

    def test_rejects_zero_capacity(self):
        with pytest.raises(ValueError, match="must be > 0"):
            ContextManager(max_items=0)

    def test_rejects_negative_capacity(self):
        with pytest.raises(ValueError, match="must be > 0"):
            ContextManager(max_items=-5)

    def test_rejects_over_max_capacity(self):
        with pytest.raises(ValueError, match=f"must be <= {MAX_CAPACITY}"):
            ContextManager(max_items=MAX_CAPACITY + 1)

    def test_accepts_max_capacity(self):
        cm = ContextManager(max_items=MAX_CAPACITY)
        assert cm.max_items == MAX_CAPACITY

    def test_accepts_one(self):
        cm = ContextManager(max_items=1)
        assert cm.max_items == 1

    def test_rejects_eviction_threshold_zero(self):
        with pytest.raises(ValueError, match="eviction_threshold"):
            ContextManager(max_items=10, eviction_threshold=0.0)

    def test_rejects_eviction_threshold_above_one(self):
        with pytest.raises(ValueError, match="eviction_threshold"):
            ContextManager(max_items=10, eviction_threshold=1.5)

    def test_rejects_negative_recency_weight(self):
        with pytest.raises(ValueError, match="recency_weight"):
            ContextManager(max_items=10, recency_weight=-0.1)

    def test_rejects_importance_weight_above_one(self):
        with pytest.raises(ValueError, match="importance_weight"):
            ContextManager(max_items=10, importance_weight=1.1)


class TestContextManagerWarmUp:
    """Test warm-up from DB."""

    def test_warm_up_loads_from_db(self, session, session_factory):
        """Warm-up should load existing memories into context."""
        # Pre-insert memories into DB
        for i in range(3):
            mem = MemoryModel(
                content_hash=f"warmup_hash_{i}",
                zone_id="test",
                user_id="alice",
                agent_id="agent1",
                importance=0.5,
                state="active",
            )
            session.add(mem)
        session.commit()

        # Create pager with warm_up=True
        pager = MemoryPager(
            session_factory=session_factory,
            zone_id="test",
            main_capacity=10,
            warm_up=True,
        )

        # Context should have the 3 pre-inserted memories
        assert pager.context.count() == 3

    def test_warm_up_respects_capacity(self, session, session_factory):
        """Warm-up should not exceed main_capacity."""
        # Pre-insert more memories than capacity
        for i in range(10):
            mem = MemoryModel(
                content_hash=f"warmup_cap_{i}",
                zone_id="test",
                user_id="alice",
                agent_id="agent1",
                importance=0.5,
                state="active",
            )
            session.add(mem)
        session.commit()

        pager = MemoryPager(
            session_factory=session_factory,
            zone_id="test",
            main_capacity=5,
            warm_up=True,
        )

        # Should only load up to capacity
        assert pager.context.count() <= 5

    def test_warm_up_skips_inactive(self, session, session_factory):
        """Warm-up should only load active memories."""
        # Insert one active, one deleted
        active = MemoryModel(
            content_hash="active_hash",
            zone_id="test",
            user_id="alice",
            agent_id="agent1",
            state="active",
        )
        deleted = MemoryModel(
            content_hash="deleted_hash",
            zone_id="test",
            user_id="alice",
            agent_id="agent1",
            state="deleted",
        )
        session.add_all([active, deleted])
        session.commit()

        pager = MemoryPager(
            session_factory=session_factory,
            zone_id="test",
            main_capacity=10,
            warm_up=True,
        )

        # Should only load the active memory
        assert pager.context.count() == 1


class TestContextManagerThreadSafety:
    """Test thread safety of ContextManager."""

    def test_concurrent_adds(self):
        """Concurrent adds should not corrupt state."""
        import threading
        import uuid

        cm = ContextManager(max_items=100, eviction_threshold=1.0)
        errors: list[Exception] = []

        def add_memories(start: int, count: int) -> None:
            try:
                for i in range(start, start + count):
                    mem = MemoryModel(
                        memory_id=str(uuid.uuid4()),
                        content_hash=f"thread_hash_{i}",
                        zone_id="test",
                        user_id="alice",
                        agent_id="agent1",
                        importance=0.5,
                    )
                    cm.add(mem)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add_memories, args=(i * 20, 20)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"
        # All 100 memories should be accounted for
        assert cm.count() == 100
        # Index, buffer, and buffer_ids should all be consistent
        assert len(cm._index) == len(cm._buffer)
        assert len(cm._buffer_ids) == len(cm._buffer)


class TestZoneIsolation:
    """Test that zones don't leak into each other."""

    def test_recall_count_isolated_by_zone(self, session_factory, session):
        """Recall counts should be scoped to zone_id."""
        pager_a = MemoryPager(
            session_factory=session_factory,
            zone_id="zone_a",
            main_capacity=3,
            warm_up=False,
        )
        pager_b = MemoryPager(
            session_factory=session_factory,
            zone_id="zone_b",
            main_capacity=3,
            warm_up=False,
        )

        # Add enough to zone_a to trigger eviction -> recall
        for i in range(5):
            mem = MemoryModel(
                content_hash=f"zone_a_{i}",
                zone_id="zone_a",
                user_id="alice",
                agent_id="agent1",
                importance=0.5,
            )
            pager_a.add_to_main(mem)

        # Zone B should have empty recall
        assert pager_b.recall.count() == 0
        assert pager_b.context.count() == 0

        # Zone A should have some in recall
        assert pager_a.recall.count() > 0

    def test_warm_up_isolated_by_zone(self, session_factory, session):
        """Warm-up should only load memories from the matching zone."""
        # Insert memories for two zones
        for i in range(3):
            session.add(
                MemoryModel(
                    content_hash=f"zone_x_{i}",
                    zone_id="zone_x",
                    user_id="alice",
                    agent_id="agent1",
                    state="active",
                )
            )
        for i in range(2):
            session.add(
                MemoryModel(
                    content_hash=f"zone_y_{i}",
                    zone_id="zone_y",
                    user_id="alice",
                    agent_id="agent1",
                    state="active",
                )
            )
        session.commit()

        pager_x = MemoryPager(
            session_factory=session_factory,
            zone_id="zone_x",
            main_capacity=10,
            warm_up=True,
        )
        pager_y = MemoryPager(
            session_factory=session_factory,
            zone_id="zone_y",
            main_capacity=10,
            warm_up=True,
        )

        assert pager_x.context.count() == 3
        assert pager_y.context.count() == 2
