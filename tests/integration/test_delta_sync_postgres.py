"""PostgreSQL integration tests for delta sync ChangeLogStore (Issue #1127).

Tests the ChangeLogStore CRUD operations against a real PostgreSQL database
to verify:
- PostgreSQL-specific upsert (ON CONFLICT DO UPDATE with named constraint)
- BRIN index compatibility
- CRUD: get, upsert, delete, get_last_sync_time
- Concurrent upserts
- Zone isolation

Prerequisites:
    docker compose --profile test up -d postgres-test

Usage:
    NEXUS_DATABASE_URL=postgresql://nexus_test:nexus_test_password@localhost:5433/nexus_test \
    pytest tests/integration/test_delta_sync_postgres.py -v --tb=short
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from nexus.services.change_log_store import ChangeLogStore
from nexus.storage.models import BackendChangeLogModel

# ============================================================================
# Skip if PostgreSQL is not available
# ============================================================================

DB_URL = os.environ.get(
    "NEXUS_DATABASE_URL",
    "postgresql://nexus_test:nexus_test_password@localhost:5433/nexus_test",
)


def is_postgres_available() -> bool:
    """Check if PostgreSQL test database is available."""
    try:
        engine = create_engine(DB_URL)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not is_postgres_available(),
    reason="PostgreSQL not available (start with: docker compose --profile test up -d postgres-test)",
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture()
def pg_engine():
    """Create a PostgreSQL engine and set up the backend_change_log table."""
    engine = create_engine(DB_URL, echo=False)

    # Create only the BackendChangeLogModel table (not all tables)
    BackendChangeLogModel.__table__.create(engine, checkfirst=True)

    yield engine

    # Clean up: drop the table after tests
    BackendChangeLogModel.__table__.drop(engine, checkfirst=True)
    engine.dispose()


@pytest.fixture()
def pg_session_factory(pg_engine):
    """Create a session factory bound to the test engine."""
    return sessionmaker(bind=pg_engine)


@pytest.fixture()
def store(pg_session_factory):
    """Create a ChangeLogStore backed by real PostgreSQL."""
    gateway = MagicMock()
    gateway.session_factory = pg_session_factory
    return ChangeLogStore(gateway)


@pytest.fixture(autouse=True)
def clean_table(pg_session_factory):
    """Clean the table before each test."""
    session = pg_session_factory()
    try:
        session.query(BackendChangeLogModel).delete()
        session.commit()
    finally:
        session.close()


# ============================================================================
# Tests
# ============================================================================


class TestChangeLogStorePostgres:
    """Test ChangeLogStore against real PostgreSQL."""

    def test_upsert_and_get(self, store: ChangeLogStore):
        """Test basic insert and retrieval."""
        now = datetime.now(UTC)
        result = store.upsert_change_log(
            path="/mnt/gcs/test.txt",
            backend_name="gcs",
            zone_id="zone-1",
            size_bytes=1024,
            mtime=now,
            backend_version="gen-123",
            content_hash="abc123",
        )
        assert result is True

        entry = store.get_change_log("/mnt/gcs/test.txt", "gcs", "zone-1")
        assert entry is not None
        assert entry.path == "/mnt/gcs/test.txt"
        assert entry.backend_name == "gcs"
        assert entry.size_bytes == 1024
        assert entry.backend_version == "gen-123"
        assert entry.content_hash == "abc123"
        assert entry.synced_at is not None

    def test_upsert_updates_existing(self, store: ChangeLogStore):
        """Test that upsert updates an existing entry (PostgreSQL ON CONFLICT)."""
        store.upsert_change_log(
            path="/mnt/gcs/test.txt",
            backend_name="gcs",
            zone_id="default",
            size_bytes=1024,
            backend_version="gen-1",
        )

        # Update with new version
        store.upsert_change_log(
            path="/mnt/gcs/test.txt",
            backend_name="gcs",
            zone_id="default",
            size_bytes=2048,
            backend_version="gen-2",
        )

        entry = store.get_change_log("/mnt/gcs/test.txt", "gcs", "default")
        assert entry is not None
        assert entry.size_bytes == 2048
        assert entry.backend_version == "gen-2"

    def test_upsert_no_duplicate_rows(self, store: ChangeLogStore, pg_session_factory):
        """Test that upsert doesn't create duplicate rows."""
        for i in range(5):
            store.upsert_change_log(
                path="/mnt/gcs/test.txt",
                backend_name="gcs",
                zone_id="default",
                size_bytes=1024 * (i + 1),
                backend_version=f"gen-{i}",
            )

        session = pg_session_factory()
        try:
            count = (
                session.query(BackendChangeLogModel)
                .filter(
                    BackendChangeLogModel.path == "/mnt/gcs/test.txt",
                    BackendChangeLogModel.backend_name == "gcs",
                )
                .count()
            )
            assert count == 1, f"Expected 1 row, got {count} (upsert created duplicates)"
        finally:
            session.close()

    def test_delete_change_log(self, store: ChangeLogStore):
        """Test deletion of change log entry."""
        store.upsert_change_log(
            path="/mnt/gcs/deleted.txt",
            backend_name="gcs",
            zone_id="default",
            size_bytes=512,
            backend_version="gen-99",
        )

        # Verify it exists
        entry = store.get_change_log("/mnt/gcs/deleted.txt", "gcs", "default")
        assert entry is not None

        # Delete it
        result = store.delete_change_log("/mnt/gcs/deleted.txt", "gcs", "default")
        assert result is True

        # Verify it's gone
        entry = store.get_change_log("/mnt/gcs/deleted.txt", "gcs", "default")
        assert entry is None

    def test_delete_nonexistent_returns_true(self, store: ChangeLogStore):
        """Test deleting a non-existent entry returns True (no error)."""
        result = store.delete_change_log("/mnt/gcs/nonexistent.txt", "gcs", "default")
        assert result is True

    def test_get_last_sync_time(self, store: ChangeLogStore):
        """Test get_last_sync_time returns the most recent sync time."""
        store.upsert_change_log(
            path="/mnt/gcs/a.txt",
            backend_name="gcs",
            zone_id="default",
            size_bytes=100,
        )
        store.upsert_change_log(
            path="/mnt/gcs/b.txt",
            backend_name="gcs",
            zone_id="default",
            size_bytes=200,
        )

        last_sync = store.get_last_sync_time("gcs", "default")
        assert last_sync is not None
        # Should be very recent (within last 10 seconds)
        assert (datetime.now(UTC).replace(tzinfo=None) - last_sync).total_seconds() < 10

    def test_get_last_sync_time_empty(self, store: ChangeLogStore):
        """Test get_last_sync_time returns None when no entries exist."""
        result = store.get_last_sync_time("nonexistent-backend", "default")
        assert result is None

    def test_zone_isolation(self, store: ChangeLogStore):
        """Test that entries are isolated by zone."""
        store.upsert_change_log(
            path="/mnt/gcs/shared.txt",
            backend_name="gcs",
            zone_id="zone-a",
            size_bytes=100,
            backend_version="gen-a",
        )
        store.upsert_change_log(
            path="/mnt/gcs/shared.txt",
            backend_name="gcs",
            zone_id="zone-b",
            size_bytes=200,
            backend_version="gen-b",
        )

        entry_a = store.get_change_log("/mnt/gcs/shared.txt", "gcs", "zone-a")
        entry_b = store.get_change_log("/mnt/gcs/shared.txt", "gcs", "zone-b")

        assert entry_a is not None
        assert entry_b is not None
        assert entry_a.size_bytes == 100
        assert entry_b.size_bytes == 200
        assert entry_a.backend_version == "gen-a"
        assert entry_b.backend_version == "gen-b"

    def test_delete_only_affects_target_zone(self, store: ChangeLogStore):
        """Test that delete only removes the entry for the specified zone."""
        store.upsert_change_log(
            path="/mnt/gcs/shared.txt",
            backend_name="gcs",
            zone_id="zone-a",
            size_bytes=100,
        )
        store.upsert_change_log(
            path="/mnt/gcs/shared.txt",
            backend_name="gcs",
            zone_id="zone-b",
            size_bytes=200,
        )

        # Delete zone-a only
        store.delete_change_log("/mnt/gcs/shared.txt", "gcs", "zone-a")

        # zone-a should be gone
        assert store.get_change_log("/mnt/gcs/shared.txt", "gcs", "zone-a") is None
        # zone-b should remain
        entry_b = store.get_change_log("/mnt/gcs/shared.txt", "gcs", "zone-b")
        assert entry_b is not None
        assert entry_b.size_bytes == 200

    def test_upsert_with_null_optional_fields(self, store: ChangeLogStore):
        """Test upsert with None values for optional fields (mtime, hash)."""
        result = store.upsert_change_log(
            path="/mnt/gcs/minimal.txt",
            backend_name="gcs",
            zone_id="default",
            size_bytes=None,
            mtime=None,
            backend_version=None,
            content_hash=None,
        )
        assert result is True

        entry = store.get_change_log("/mnt/gcs/minimal.txt", "gcs", "default")
        assert entry is not None
        assert entry.size_bytes is None
        assert entry.mtime is None
        assert entry.backend_version is None
        assert entry.content_hash is None

    def test_unique_constraint_enforced(self, store: ChangeLogStore, pg_session_factory):
        """Test that the unique constraint (path, backend_name, zone_id) is enforced."""
        # Insert directly via session to bypass upsert
        session = pg_session_factory()
        try:
            import uuid

            entry = BackendChangeLogModel(
                id=str(uuid.uuid4()),
                path="/mnt/gcs/dup.txt",
                backend_name="gcs",
                zone_id="default",
                size_bytes=100,
                synced_at=datetime.now(UTC),
            )
            session.add(entry)
            session.commit()

            # Try to insert a duplicate
            from sqlalchemy.exc import IntegrityError

            entry2 = BackendChangeLogModel(
                id=str(uuid.uuid4()),
                path="/mnt/gcs/dup.txt",
                backend_name="gcs",
                zone_id="default",
                size_bytes=200,
                synced_at=datetime.now(UTC),
            )
            session.add(entry2)
            with pytest.raises(IntegrityError):
                session.commit()
        finally:
            session.rollback()
            session.close()

    def test_brin_index_exists(self, pg_engine):
        """Verify BRIN index was created on the synced_at column."""
        with pg_engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE tablename = 'backend_change_log' "
                    "AND indexname = 'idx_bcl_synced_brin'"
                )
            )
            rows = result.fetchall()
            assert len(rows) == 1, "BRIN index idx_bcl_synced_brin not found"
            assert "brin" in rows[0][1].lower(), f"Expected BRIN index, got: {rows[0][1]}"
