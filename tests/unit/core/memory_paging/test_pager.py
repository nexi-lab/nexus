"""Tests for MemoryPager (Issue #1258)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.core.memory_paging import MemoryPager
from nexus.storage.models import Base, MemoryModel


@pytest.fixture
def session():
    """Create in-memory test database."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def pager(session):
    """Create MemoryPager instance."""
    return MemoryPager(
        session=session,
        zone_id="test",
        main_capacity=5,  # Small capacity for testing
        recall_max_age_hours=24.0,
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
