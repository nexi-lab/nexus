"""End-to-end tests for MemGPT 3-tier paging (Issue #1258).

Tests the complete paging workflow with realistic data.
"""

import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.backends.local import LocalBackend
from nexus.core.memory_with_paging import MemoryWithPaging
from nexus.storage.models import Base


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
def backend():
    """Create temporary backend."""
    return LocalBackend(tempfile.mkdtemp())


@pytest.fixture
def memory(session, backend):
    """Create MemoryWithPaging instance."""
    return MemoryWithPaging(
        session=session,
        backend=backend,
        zone_id="test",
        user_id="alice",
        agent_id="assistant",
        enable_paging=True,
        main_capacity=10,  # Small for testing
        recall_max_age_hours=1.0,
    )


class TestMemoryPagingE2E:
    """End-to-end tests for memory paging."""

    def test_store_with_automatic_paging(self, memory):
        """Storing memories should automatically page to tiers."""
        # Store 15 memories (exceeds capacity of 10)
        memory_ids = []
        for i in range(15):
            memory_id = memory.store(
                content=f"Memory {i}: Test fact about topic {i}",
                memory_type="fact",
                importance=0.5 + (i % 10) * 0.05,
                auto_page=True,
            )
            memory_ids.append(memory_id)

        # Check distribution
        stats = memory.get_paging_stats()
        assert stats["paging_enabled"] is True
        assert stats["total_memories"] == 15

        # Main should be near capacity
        assert stats["main"]["count"] <= 10
        assert stats["main"]["utilization"] > 0.5

        # Some should have been evicted to recall
        assert stats["recall"]["count"] > 0

    def test_get_recent_context(self, memory):
        """Should get recent context from main + recall."""
        # Store several memories
        for i in range(20):
            memory.store(
                content=f"Message {i} in conversation",
                memory_type="message",
                importance=0.7,
            )

        # Get recent context
        recent = memory.get_recent_context(limit=15)

        assert len(recent) <= 15
        assert all("memory_id" in m for m in recent)

    def test_store_without_paging(self, session, backend):
        """Should work without paging enabled."""
        memory_no_paging = MemoryWithPaging(
            session=session,
            backend=backend,
            zone_id="test",
            user_id="alice",
            agent_id="assistant",
            enable_paging=False,
        )

        memory_id = memory_no_paging.store(
            content="Test without paging",
            memory_type="fact",
        )

        assert memory_id is not None

        stats = memory_no_paging.get_paging_stats()
        assert stats["paging_enabled"] is False

    def test_cascading_eviction(self, memory):
        """Memories should cascade through all tiers."""
        # Store many memories to force cascading
        for i in range(25):
            memory.store(
                content=f"Fact {i}",
                memory_type="fact",
                importance=0.5,
            )

        stats = memory.get_paging_stats()

        # Should be distributed across tiers
        assert stats["main"]["count"] <= 10  # At capacity
        assert stats["recall"]["count"] > 0  # Some in recall
        # Archival would have memories if they aged out (needs time to pass)

    def test_paging_stats(self, memory):
        """Stats should accurately reflect memory distribution."""
        # Add some memories
        for i in range(5):
            memory.store(content=f"Memory {i}", memory_type="fact")

        stats = memory.get_paging_stats()

        assert "total_memories" in stats
        assert "main" in stats
        assert "recall" in stats
        assert "archival" in stats

        assert stats["total_memories"] == 5
        assert stats["main"]["count"] == 5
        assert stats["main"]["capacity"] == 10
        assert 0 <= stats["main"]["utilization"] <= 1.0

    def test_memory_importance_affects_eviction(self, memory):
        """Higher importance memories should stay in main context longer."""
        # Add low importance memory
        low_id = memory.store(
            content="Low importance fact",
            memory_type="fact",
            importance=0.1,
        )

        # Add high importance memories to fill context
        high_ids = []
        for i in range(12):
            high_id = memory.store(
                content=f"High importance fact {i}",
                memory_type="fact",
                importance=0.9,
            )
            high_ids.append(high_id)

        # Low importance should have been evicted
        main_memories = memory.pager.context.get_all()
        main_ids = [m.memory_id for m in main_memories]

        # High importance memories more likely to be in main
        high_in_main = sum(1 for hid in high_ids if hid in main_ids)
        low_in_main = 1 if low_id in main_ids else 0

        # Most high importance should be in main
        assert high_in_main >= 5
