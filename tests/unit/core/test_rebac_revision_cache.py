"""Integration tests for revision-based cache quantization (Issue #909).

Tests verify that:
- Writes increment zone revision
- Cache keys use revision buckets
- Cache entries survive writes within same bucket
- Cache invalidates on bucket boundary crossing
- Zone isolation works with revisions

Requirements:
    - PostgreSQL running at postgresql://postgres:nexus@localhost:5432/nexus
    - Start with: docker compose -f docker-compose.demo.yml up postgres -d
    - Or set NEXUS_DATABASE_URL environment variable
"""

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from nexus.core.rebac_manager import ReBACManager
from nexus.storage.models import Base


def _check_postgres_available():
    """Check if PostgreSQL is available for testing."""
    db_url = os.getenv("NEXUS_DATABASE_URL", "postgresql://postgres:nexus@localhost:5432/nexus")
    try:
        engine = create_engine(db_url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


# Skip all tests in this module if PostgreSQL is not available
pytestmark = pytest.mark.skipif(
    not _check_postgres_available(), reason="PostgreSQL not available at localhost:5432"
)


@pytest.fixture
def engine():
    """Create PostgreSQL engine for testing.

    Drops and recreates rebac tables to handle schema drift (e.g. tenant_id → zone_id
    rename). Base.metadata.create_all() won't alter existing tables, so stale schemas
    cause column-not-found errors.
    """
    db_url = os.getenv("NEXUS_DATABASE_URL", "postgresql://postgres:nexus@localhost:5432/nexus")
    engine = create_engine(db_url)

    # Drop stale rebac tables so create_all recreates them with current schema
    # (handles tenant_id → zone_id rename and other schema drift)
    with engine.connect() as conn:
        for table in [
            "rebac_version_sequences",
            "rebac_tuples",
            "rebac_changelog",
            "rebac_group_closure",
            "rebac_check_cache",
            "rebac_namespaces",
        ]:
            conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        conn.commit()

    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def test_zone():
    """Generate unique zone ID for test isolation."""
    return f"test_rev_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def manager(engine, test_zone):
    """Create a ReBAC manager with small revision window for testing."""
    manager = ReBACManager(
        engine=engine,
        l1_cache_revision_window=5,  # Small window for testing
    )
    yield manager

    # Cleanup test data
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM rebac_tuples WHERE zone_id = :zone"), {"zone": test_zone})
        conn.execute(
            text("DELETE FROM rebac_version_sequences WHERE zone_id = :zone"),
            {"zone": test_zone},
        )
        conn.commit()

    manager.close()


class TestRevisionCacheIntegration:
    """Integration tests for revision-based cache quantization."""

    def test_write_increments_revision(self, manager, test_zone):
        """Verify each write increments zone revision."""
        initial = manager._get_zone_revision(test_zone)

        manager.rebac_write(
            subject=("agent", "alice"),
            relation="viewer",
            object=("file", "/doc.txt"),
            zone_id=test_zone,
        )

        new_rev = manager._get_zone_revision(test_zone)
        assert new_rev == initial + 1

    def test_write_increments_specific_zone(self, manager, test_zone):
        """Verify writes increment correct zone revision."""
        other_zone = f"{test_zone}_other"
        initial_t1 = manager._get_zone_revision(test_zone)
        initial_t2 = manager._get_zone_revision(other_zone)

        manager.rebac_write(
            subject=("agent", "alice"),
            relation="viewer",
            object=("file", "/doc.txt"),
            zone_id=test_zone,
        )

        # test_zone should be incremented
        assert manager._get_zone_revision(test_zone) == initial_t1 + 1
        # other_zone should be unchanged
        assert manager._get_zone_revision(other_zone) == initial_t2

    def test_batch_write_single_increment(self, manager, test_zone):
        """Batch write should increment revision once, not per-tuple."""
        initial = manager._get_zone_revision(test_zone)

        manager.rebac_write_batch(
            tuples=[
                {
                    "subject": ("agent", "alice"),
                    "relation": "viewer",
                    "object": ("file", "/doc1.txt"),
                    "zone_id": test_zone,
                },
                {
                    "subject": ("agent", "alice"),
                    "relation": "viewer",
                    "object": ("file", "/doc2.txt"),
                    "zone_id": test_zone,
                },
                {
                    "subject": ("agent", "alice"),
                    "relation": "viewer",
                    "object": ("file", "/doc3.txt"),
                    "zone_id": test_zone,
                },
            ]
        )

        final = manager._get_zone_revision(test_zone)
        assert final == initial + 1, "Batch should increment revision once"

    def test_delete_increments_revision(self, manager, test_zone):
        """Verify delete operations increment revision."""
        # First write a tuple
        tuple_id = manager.rebac_write(
            subject=("agent", "alice"),
            relation="viewer",
            object=("file", "/doc.txt"),
            zone_id=test_zone,
        )

        initial = manager._get_zone_revision(test_zone)

        # Delete it
        manager.rebac_delete(tuple_id)

        # Revision should be incremented
        assert manager._get_zone_revision(test_zone) == initial + 1

    def test_cache_key_uses_revision_bucket(self, manager, test_zone):
        """Verify cache keys use revision bucket format."""
        # Write to create some revisions
        manager.rebac_write(
            subject=("agent", "alice"),
            relation="viewer",
            object=("file", "/doc"),
            zone_id=test_zone,
        )

        # Check the cache key format
        key = manager._l1_cache._make_key("agent", "alice", "viewer", "file", "/doc", test_zone)

        # Key should end with :r{bucket} format (revision-based)
        assert key.endswith(":r0") or key.endswith(":r1"), f"Key should end with :rN, got: {key}"

    def test_cache_stable_within_revision_window(self, manager, test_zone):
        """Cache entries survive writes within same revision bucket."""
        # Setup: write permission tuple
        manager.rebac_write(
            subject=("agent", "alice"),
            relation="member-of",
            object=("group", "eng-team"),
            zone_id=test_zone,
        )

        # First check - populates cache (using relation as permission)
        result1 = manager.rebac_check(
            subject=("agent", "alice"),
            permission="member-of",
            object=("group", "eng-team"),
            zone_id=test_zone,
        )
        assert result1 is True

        hits_before = manager._l1_cache.get_stats()["hits"]

        # Write 3 more tuples (still within window of 5)
        for i in range(3):
            manager.rebac_write(
                subject=("agent", f"user{i}"),
                relation="member-of",
                object=("group", f"team{i}"),
                zone_id=test_zone,
            )

        # Clear local revision cache to get fresh bucket
        manager._l1_cache._revision_cache.clear()

        # Check again - should still hit cache (within same revision bucket)
        result2 = manager.rebac_check(
            subject=("agent", "alice"),
            permission="member-of",
            object=("group", "eng-team"),
            zone_id=test_zone,
        )
        assert result2 is True

        hits_after = manager._l1_cache.get_stats()["hits"]
        assert hits_after > hits_before, "Should hit cache within revision window"

    def test_cache_invalidates_on_bucket_change(self, manager, test_zone):
        """Cache misses when revision bucket changes."""
        # Setup
        manager.rebac_write(
            subject=("agent", "alice"),
            relation="member-of",
            object=("group", "eng-team"),
            zone_id=test_zone,
        )
        manager.rebac_check(
            subject=("agent", "alice"),
            permission="member-of",
            object=("group", "eng-team"),
            zone_id=test_zone,
        )

        misses_before = manager._l1_cache.get_stats()["misses"]

        # Write enough to cross bucket boundary (window=5)
        for i in range(6):
            manager.rebac_write(
                subject=("agent", f"filler{i}"),
                relation="member-of",
                object=("group", f"team{i}"),
                zone_id=test_zone,
            )

        # Clear local revision cache to get fresh bucket
        manager._l1_cache._revision_cache.clear()

        # Should miss cache (new revision bucket)
        manager.rebac_check(
            subject=("agent", "alice"),
            permission="member-of",
            object=("group", "eng-team"),
            zone_id=test_zone,
        )

        misses_after = manager._l1_cache.get_stats()["misses"]
        assert misses_after > misses_before, "Should miss after bucket change"

    def test_zone_isolation_with_revisions(self, manager, test_zone):
        """Different zones have independent revision tracking."""
        other_zone = f"{test_zone}_other"
        # Write to test_zone multiple times to advance its revision
        for i in range(10):
            manager.rebac_write(
                subject=("agent", f"user{i}"),
                relation="member-of",
                object=("group", f"team{i}"),
                zone_id=test_zone,
            )

        # other_zone should still be at revision 0
        assert manager._get_zone_revision(other_zone) == 0

    def test_revision_fetcher_connected(self, manager, test_zone):
        """Verify revision fetcher callback is properly connected."""
        # The fetcher should return data from the database
        assert manager._l1_cache._revision_fetcher is not None

        # Write to create a revision
        manager.rebac_write(
            subject=("agent", "alice"),
            relation="member-of",
            object=("group", "eng-team"),
            zone_id=test_zone,
        )

        # Fetcher should return the updated revision
        revision = manager._l1_cache._revision_fetcher(test_zone)
        assert revision >= 1

    def test_cache_hit_rate_with_revision_quantization(self, manager, test_zone):
        """Verify cache achieves good hit rate with revision quantization."""
        # Setup: create some permissions
        for i in range(5):
            manager.rebac_write(
                subject=("agent", f"user{i}"),
                relation="member-of",
                object=("group", f"team{i}"),
                zone_id=test_zone,
            )

        # Reset stats
        manager._l1_cache.reset_stats()

        # Simulate read-heavy workload (100 checks, 5 unique)
        for _ in range(20):
            for i in range(5):
                manager.rebac_check(
                    subject=("agent", f"user{i}"),
                    permission="member-of",
                    object=("group", f"team{i}"),
                    zone_id=test_zone,
                )

        stats = manager._l1_cache.get_stats()
        hit_rate = stats["hit_rate_percent"]

        # Should achieve >80% hit rate (95 hits out of 100 checks after warmup)
        assert hit_rate > 80, f"Expected >80% hit rate, got {hit_rate}%"
