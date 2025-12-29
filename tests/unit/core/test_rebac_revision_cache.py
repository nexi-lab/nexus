"""Integration tests for revision-based cache quantization (Issue #909).

Tests verify that:
- Writes increment tenant revision
- Cache keys use revision buckets
- Cache entries survive writes within same bucket
- Cache invalidates on bucket boundary crossing
- Tenant isolation works with revisions

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


@pytest.fixture
def engine():
    """Create PostgreSQL engine for testing."""
    db_url = os.getenv(
        "NEXUS_DATABASE_URL",
        "postgresql://postgres:nexus@localhost:5432/nexus"
    )
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def test_tenant():
    """Generate unique tenant ID for test isolation."""
    return f"test_rev_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def manager(engine, test_tenant):
    """Create a ReBAC manager with small revision window for testing."""
    manager = ReBACManager(
        engine=engine,
        l1_cache_revision_window=5,  # Small window for testing
    )
    yield manager

    # Cleanup test data
    with engine.connect() as conn:
        conn.execute(
            text("DELETE FROM rebac_tuples WHERE tenant_id = :tenant"),
            {"tenant": test_tenant}
        )
        conn.execute(
            text("DELETE FROM rebac_version_sequences WHERE tenant_id = :tenant"),
            {"tenant": test_tenant}
        )
        conn.commit()

    manager.close()


class TestRevisionCacheIntegration:
    """Integration tests for revision-based cache quantization."""

    def test_write_increments_revision(self, manager, test_tenant):
        """Verify each write increments tenant revision."""
        initial = manager._get_tenant_revision(test_tenant)

        manager.rebac_write(
            subject=("agent", "alice"),
            relation="viewer",
            object=("file", "/doc.txt"),
            tenant_id=test_tenant,
        )

        new_rev = manager._get_tenant_revision(test_tenant)
        assert new_rev == initial + 1

    def test_write_increments_specific_tenant(self, manager, test_tenant):
        """Verify writes increment correct tenant revision."""
        other_tenant = f"{test_tenant}_other"
        initial_t1 = manager._get_tenant_revision(test_tenant)
        initial_t2 = manager._get_tenant_revision(other_tenant)

        manager.rebac_write(
            subject=("agent", "alice"),
            relation="viewer",
            object=("file", "/doc.txt"),
            tenant_id=test_tenant,
        )

        # test_tenant should be incremented
        assert manager._get_tenant_revision(test_tenant) == initial_t1 + 1
        # other_tenant should be unchanged
        assert manager._get_tenant_revision(other_tenant) == initial_t2

    def test_batch_write_single_increment(self, manager, test_tenant):
        """Batch write should increment revision once, not per-tuple."""
        initial = manager._get_tenant_revision(test_tenant)

        manager.rebac_write_batch(
            tuples=[
                {
                    "subject": ("agent", "alice"),
                    "relation": "viewer",
                    "object": ("file", "/doc1.txt"),
                    "tenant_id": test_tenant,
                },
                {
                    "subject": ("agent", "alice"),
                    "relation": "viewer",
                    "object": ("file", "/doc2.txt"),
                    "tenant_id": test_tenant,
                },
                {
                    "subject": ("agent", "alice"),
                    "relation": "viewer",
                    "object": ("file", "/doc3.txt"),
                    "tenant_id": test_tenant,
                },
            ]
        )

        final = manager._get_tenant_revision(test_tenant)
        assert final == initial + 1, "Batch should increment revision once"

    def test_delete_increments_revision(self, manager, test_tenant):
        """Verify delete operations increment revision."""
        # First write a tuple
        tuple_id = manager.rebac_write(
            subject=("agent", "alice"),
            relation="viewer",
            object=("file", "/doc.txt"),
            tenant_id=test_tenant,
        )

        initial = manager._get_tenant_revision(test_tenant)

        # Delete it
        manager.rebac_delete(tuple_id)

        # Revision should be incremented
        assert manager._get_tenant_revision(test_tenant) == initial + 1

    def test_cache_key_uses_revision_bucket(self, manager, test_tenant):
        """Verify cache keys use revision bucket format."""
        # Write to create some revisions
        manager.rebac_write(
            subject=("agent", "alice"),
            relation="viewer",
            object=("file", "/doc"),
            tenant_id=test_tenant,
        )

        # Check the cache key format
        key = manager._l1_cache._make_key(
            "agent", "alice", "viewer", "file", "/doc", test_tenant
        )

        # Key should end with :r{bucket} format (revision-based)
        assert key.endswith(":r0") or key.endswith(":r1"), f"Key should end with :rN, got: {key}"

    def test_cache_stable_within_revision_window(self, manager, test_tenant):
        """Cache entries survive writes within same revision bucket."""
        # Setup: write permission tuple
        manager.rebac_write(
            subject=("agent", "alice"),
            relation="member-of",
            object=("group", "eng-team"),
            tenant_id=test_tenant,
        )

        # First check - populates cache (using relation as permission)
        result1 = manager.rebac_check(
            subject=("agent", "alice"),
            permission="member-of",
            object=("group", "eng-team"),
            tenant_id=test_tenant,
        )
        assert result1 is True

        hits_before = manager._l1_cache.get_stats()["hits"]

        # Write 3 more tuples (still within window of 5)
        for i in range(3):
            manager.rebac_write(
                subject=("agent", f"user{i}"),
                relation="member-of",
                object=("group", f"team{i}"),
                tenant_id=test_tenant,
            )

        # Clear local revision cache to get fresh bucket
        manager._l1_cache._revision_cache.clear()

        # Check again - should still hit cache (within same revision bucket)
        result2 = manager.rebac_check(
            subject=("agent", "alice"),
            permission="member-of",
            object=("group", "eng-team"),
            tenant_id=test_tenant,
        )
        assert result2 is True

        hits_after = manager._l1_cache.get_stats()["hits"]
        assert hits_after > hits_before, "Should hit cache within revision window"

    def test_cache_invalidates_on_bucket_change(self, manager, test_tenant):
        """Cache misses when revision bucket changes."""
        # Setup
        manager.rebac_write(
            subject=("agent", "alice"),
            relation="member-of",
            object=("group", "eng-team"),
            tenant_id=test_tenant,
        )
        manager.rebac_check(
            subject=("agent", "alice"),
            permission="member-of",
            object=("group", "eng-team"),
            tenant_id=test_tenant,
        )

        misses_before = manager._l1_cache.get_stats()["misses"]

        # Write enough to cross bucket boundary (window=5)
        for i in range(6):
            manager.rebac_write(
                subject=("agent", f"filler{i}"),
                relation="member-of",
                object=("group", f"team{i}"),
                tenant_id=test_tenant,
            )

        # Clear local revision cache to get fresh bucket
        manager._l1_cache._revision_cache.clear()

        # Should miss cache (new revision bucket)
        manager.rebac_check(
            subject=("agent", "alice"),
            permission="member-of",
            object=("group", "eng-team"),
            tenant_id=test_tenant,
        )

        misses_after = manager._l1_cache.get_stats()["misses"]
        assert misses_after > misses_before, "Should miss after bucket change"

    def test_tenant_isolation_with_revisions(self, manager, test_tenant):
        """Different tenants have independent revision tracking."""
        other_tenant = f"{test_tenant}_other"
        # Write to test_tenant multiple times to advance its revision
        for i in range(10):
            manager.rebac_write(
                subject=("agent", f"user{i}"),
                relation="member-of",
                object=("group", f"team{i}"),
                tenant_id=test_tenant,
            )

        # other_tenant should still be at revision 0
        assert manager._get_tenant_revision(other_tenant) == 0

    def test_revision_fetcher_connected(self, manager, test_tenant):
        """Verify revision fetcher callback is properly connected."""
        # The fetcher should return data from the database
        assert manager._l1_cache._revision_fetcher is not None

        # Write to create a revision
        manager.rebac_write(
            subject=("agent", "alice"),
            relation="member-of",
            object=("group", "eng-team"),
            tenant_id=test_tenant,
        )

        # Fetcher should return the updated revision
        revision = manager._l1_cache._revision_fetcher(test_tenant)
        assert revision >= 1

    def test_cache_hit_rate_with_revision_quantization(self, manager, test_tenant):
        """Verify cache achieves good hit rate with revision quantization."""
        # Setup: create some permissions
        for i in range(5):
            manager.rebac_write(
                subject=("agent", f"user{i}"),
                relation="member-of",
                object=("group", f"team{i}"),
                tenant_id=test_tenant,
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
                    tenant_id=test_tenant,
                )

        stats = manager._l1_cache.get_stats()
        hit_rate = stats["hit_rate_percent"]

        # Should achieve >80% hit rate (95 hits out of 100 checks after warmup)
        assert hit_rate > 80, f"Expected >80% hit rate, got {hit_rate}%"
