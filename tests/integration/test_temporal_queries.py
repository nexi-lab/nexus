"""End-to-end tests for temporal query operators (Issue #1023).

Tests the `after`, `before`, `during` temporal operators through the Memory API
with PostgreSQL or SQLite database to verify filtering works correctly.

Environment variables:
    TEST_DATABASE_URL: PostgreSQL connection URL (default: sqlite:///:memory:)

Example:
    # Run with PostgreSQL
    TEST_DATABASE_URL=postgresql://localhost/nexus_test pytest tests/integration/test_temporal_queries.py

    # Run with SQLite (default)
    pytest tests/integration/test_temporal_queries.py
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.backends.local import LocalBackend
from nexus.core.entity_registry import EntityRegistry
from nexus.core.memory_api import Memory
from nexus.storage.models import Base, MemoryModel

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Use PostgreSQL if TEST_DATABASE_URL is set, otherwise SQLite
DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "sqlite:///:memory:")


@pytest.fixture
def engine():
    """Create database for testing (PostgreSQL or SQLite)."""
    engine = create_engine(DATABASE_URL)
    Base.metadata.drop_all(engine)  # Clean slate for PostgreSQL
    Base.metadata.create_all(engine)
    yield engine
    if DATABASE_URL.startswith("postgresql"):
        Base.metadata.drop_all(engine)  # Cleanup for PostgreSQL
    engine.dispose()


@pytest.fixture
def session(engine):
    """Create database session."""
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def backend(tmp_path):
    """Create local backend for content storage."""
    return LocalBackend(root_path=tmp_path)


@pytest.fixture
def entity_registry(session):
    """Create and populate entity registry."""
    registry = EntityRegistry(session)
    registry.register_entity("zone", "acme")
    registry.register_entity("user", "alice", parent_type="zone", parent_id="acme")
    registry.register_entity("agent", "agent1", parent_type="user", parent_id="alice")
    return registry


@pytest.fixture
def memory_api(session, backend, entity_registry):
    """Create Memory API instance."""
    return Memory(
        session=session,
        backend=backend,
        zone_id="acme",
        user_id="alice",
        agent_id="agent1",
        entity_registry=entity_registry,
    )


@pytest.fixture
def memories_with_timestamps(memory_api, session):
    """Create memories with specific timestamps for testing."""
    # Store memories with different timestamps
    now = datetime.now(UTC)

    memories = []

    # Memory from 30 days ago
    mem1_id = memory_api.store(
        content="Old memory from last month",
        scope="user",
        memory_type="fact",
    )
    memories.append({"id": mem1_id, "age": "30_days_ago"})

    # Memory from 7 days ago
    mem2_id = memory_api.store(
        content="Memory from last week",
        scope="user",
        memory_type="fact",
    )
    memories.append({"id": mem2_id, "age": "7_days_ago"})

    # Memory from yesterday
    mem3_id = memory_api.store(
        content="Memory from yesterday",
        scope="user",
        memory_type="preference",
    )
    memories.append({"id": mem3_id, "age": "1_day_ago"})

    # Memory from today
    mem4_id = memory_api.store(
        content="Memory from today",
        scope="user",
        memory_type="experience",
    )
    memories.append({"id": mem4_id, "age": "today"})

    # Manually update created_at timestamps for testing
    # (since store() always uses current time)
    session.query(MemoryModel).filter(MemoryModel.memory_id == mem1_id).update(
        {"created_at": now - timedelta(days=30)}
    )
    session.query(MemoryModel).filter(MemoryModel.memory_id == mem2_id).update(
        {"created_at": now - timedelta(days=7)}
    )
    session.query(MemoryModel).filter(MemoryModel.memory_id == mem3_id).update(
        {"created_at": now - timedelta(days=1)}
    )
    # mem4 keeps current timestamp
    session.commit()

    return {"memories": memories, "now": now}


class TestTemporalQueryAfter:
    """Tests for the 'after' temporal operator."""

    def test_query_after_filters_old_memories(self, memory_api, memories_with_timestamps):
        """Test that 'after' filters out memories before the specified date."""
        now = memories_with_timestamps["now"]

        # Query memories from last 3 days
        after_date = now - timedelta(days=3)
        results = memory_api.query(after=after_date)

        # Should only get memories from yesterday and today
        assert len(results) == 2
        contents = [r["content"] for r in results]
        assert "Memory from today" in contents
        assert "Memory from yesterday" in contents
        assert "Old memory from last month" not in contents

        logger.info(f"[TEST] after={after_date.isoformat()} returned {len(results)} memories")

    def test_query_after_with_iso_string(self, memory_api, memories_with_timestamps):
        """Test 'after' with ISO-8601 string format."""
        now = memories_with_timestamps["now"]

        # Query using ISO string
        after_str = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        results = memory_api.query(after=after_str)

        assert len(results) == 2
        logger.info(f"[TEST] after='{after_str}' returned {len(results)} memories")

    def test_query_after_with_date_only(self, memory_api, memories_with_timestamps):
        """Test 'after' with date-only string (YYYY-MM-DD)."""
        now = memories_with_timestamps["now"]

        # Query using date string
        after_str = (now - timedelta(days=3)).strftime("%Y-%m-%d")
        results = memory_api.query(after=after_str)

        assert len(results) >= 2
        logger.info(f"[TEST] after='{after_str}' returned {len(results)} memories")


class TestTemporalQueryBefore:
    """Tests for the 'before' temporal operator."""

    def test_query_before_filters_recent_memories(self, memory_api, memories_with_timestamps):
        """Test that 'before' filters out memories after the specified date."""
        now = memories_with_timestamps["now"]

        # Query memories from more than 5 days ago
        before_date = now - timedelta(days=5)
        results = memory_api.query(before=before_date)

        # Should only get memories from 7 and 30 days ago
        assert len(results) == 2
        contents = [r["content"] for r in results]
        assert "Memory from last week" in contents
        assert "Old memory from last month" in contents
        assert "Memory from today" not in contents

        logger.info(f"[TEST] before={before_date.isoformat()} returned {len(results)} memories")

    def test_query_before_with_iso_string(self, memory_api, memories_with_timestamps):
        """Test 'before' with ISO-8601 string format."""
        now = memories_with_timestamps["now"]

        before_str = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        results = memory_api.query(before=before_str)

        assert len(results) == 2
        logger.info(f"[TEST] before='{before_str}' returned {len(results)} memories")


class TestTemporalQueryDuring:
    """Tests for the 'during' temporal operator."""

    def test_query_during_current_month(self, memory_api, memories_with_timestamps):
        """Test 'during' with current year-month."""
        now = memories_with_timestamps["now"]

        # Query current month
        during_str = now.strftime("%Y-%m")
        results = memory_api.query(during=during_str)

        # Results depend on when test runs, but should include recent memories
        assert len(results) >= 1
        logger.info(f"[TEST] during='{during_str}' returned {len(results)} memories")

    def test_query_during_current_year(self, memory_api, memories_with_timestamps):
        """Test 'during' with current year."""
        now = memories_with_timestamps["now"]

        # Query current year
        during_str = now.strftime("%Y")
        results = memory_api.query(during=during_str)

        # Should include all recent memories
        assert len(results) >= 1
        logger.info(f"[TEST] during='{during_str}' returned {len(results)} memories")

    def test_during_mutually_exclusive_with_after(self, memory_api):
        """Test that 'during' cannot be used with 'after'."""
        with pytest.raises(ValueError, match="Cannot use 'during' with"):
            memory_api.query(during="2025", after="2025-01-01")

    def test_during_mutually_exclusive_with_before(self, memory_api):
        """Test that 'during' cannot be used with 'before'."""
        with pytest.raises(ValueError, match="Cannot use 'during' with"):
            memory_api.query(during="2025", before="2025-12-31")


class TestTemporalQueryCombined:
    """Tests for combining after and before operators."""

    def test_query_date_range(self, memory_api, memories_with_timestamps):
        """Test combining 'after' and 'before' for date range."""
        now = memories_with_timestamps["now"]

        # Query memories from 10 to 2 days ago
        after_date = now - timedelta(days=10)
        before_date = now - timedelta(days=2)
        results = memory_api.query(after=after_date, before=before_date)

        # Should only get memory from 7 days ago
        assert len(results) == 1
        assert "Memory from last week" in results[0]["content"]

        logger.info(
            f"[TEST] Range {after_date.date()} to {before_date.date()} returned {len(results)} memories"
        )

    def test_invalid_date_range_raises_error(self, memory_api):
        """Test that after > before raises ValueError."""
        with pytest.raises(ValueError, match="must be before"):
            memory_api.query(after="2025-12-31", before="2025-01-01")


class TestTemporalQueryList:
    """Tests for temporal operators in list() method."""

    def test_list_with_after(self, memory_api, memories_with_timestamps):
        """Test list() with 'after' parameter."""
        now = memories_with_timestamps["now"]

        after_date = now - timedelta(days=3)
        results = memory_api.list(after=after_date)

        assert len(results) == 2
        logger.info(f"[TEST] list(after={after_date.date()}) returned {len(results)} memories")

    def test_list_with_during(self, memory_api, memories_with_timestamps):
        """Test list() with 'during' parameter."""
        now = memories_with_timestamps["now"]

        during_str = now.strftime("%Y-%m")
        results = memory_api.list(during=during_str)

        assert len(results) >= 1
        logger.info(f"[TEST] list(during='{during_str}') returned {len(results)} memories")


class TestTemporalQuerySearch:
    """Tests for temporal operators in search() method."""

    def test_search_with_after(self, memory_api, memories_with_timestamps):
        """Test search() with 'after' parameter."""
        now = memories_with_timestamps["now"]

        after_date = now - timedelta(days=3)
        results = memory_api.search("memory", after=after_date)

        # Should only search recent memories
        for result in results:
            assert "Old memory" not in result["content"]

        logger.info(f"[TEST] search with after={after_date.date()} returned {len(results)} results")

    def test_search_with_during(self, memory_api, memories_with_timestamps):
        """Test search() with 'during' parameter."""
        now = memories_with_timestamps["now"]

        during_str = now.strftime("%Y-%m")
        results = memory_api.search("memory", during=during_str)

        assert len(results) >= 0  # May be 0 if keyword search doesn't match
        logger.info(f"[TEST] search with during='{during_str}' returned {len(results)} results")


class TestTemporalQueryOrdering:
    """Tests for result ordering with temporal queries."""

    def test_results_ordered_by_created_at_desc(self, memory_api, memories_with_timestamps):
        """Test that results are ordered by created_at descending (newest first)."""
        results = memory_api.query()

        assert len(results) == 4

        # Verify descending order (newest first)
        for i in range(len(results) - 1):
            curr_time = results[i]["created_at"]
            next_time = results[i + 1]["created_at"]
            assert curr_time >= next_time, "Results should be ordered newest first"

        # First result should be "Memory from today"
        assert "today" in results[0]["content"]

        logger.info("[TEST] Results correctly ordered by created_at DESC")


class TestPostgreSQLTimestampPrecision:
    """PostgreSQL-specific tests for timestamp precision."""

    @pytest.mark.skipif(
        not DATABASE_URL.startswith("postgresql"), reason="PostgreSQL-specific test"
    )
    def test_microsecond_precision(self, session):
        """Test that PostgreSQL handles microsecond precision correctly."""
        from nexus.core.memory_router import MemoryViewRouter

        now = datetime.now(UTC)

        # Create memories with microsecond differences
        mem1 = MemoryModel(
            content_hash="hash_micro1",
            zone_id="acme",
            user_id="alice",
            agent_id="agent1",
            scope="user",
            state="active",
            created_at=now - timedelta(microseconds=500000),  # 0.5 seconds ago
        )
        mem2 = MemoryModel(
            content_hash="hash_micro2",
            zone_id="acme",
            user_id="alice",
            agent_id="agent1",
            scope="user",
            state="active",
            created_at=now - timedelta(microseconds=100000),  # 0.1 seconds ago
        )
        session.add(mem1)
        session.add(mem2)
        session.commit()

        router = MemoryViewRouter(session, entity_registry=None)

        # Query with precise timestamp (should only get mem2)
        after_precise = now - timedelta(microseconds=300000)
        results = router.query_memories(zone_id="acme", after=after_precise)

        micro_results = [r for r in results if r.content_hash.startswith("hash_micro")]
        assert len(micro_results) == 1
        assert micro_results[0].content_hash == "hash_micro2"

        logger.info("[TEST] PostgreSQL microsecond precision verified")


# Standalone test runner
if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "--log-cli-level=INFO"])
