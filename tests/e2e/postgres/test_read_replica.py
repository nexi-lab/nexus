"""E2E tests for read/write replica separation (Issue #725).

These tests require a real PostgreSQL read replica and are skipped unless
NEXUS_READ_REPLICA_URL is set in the environment.

Setup:
    export NEXUS_DATABASE_URL=postgresql://postgres:nexus@localhost:5432/nexus
    export NEXUS_READ_REPLICA_URL=postgresql://postgres:nexus@localhost:5433/nexus
    pytest tests/e2e/postgres/test_read_replica.py -v
"""

import os

import pytest

# Skip all tests unless read replica is configured
pytestmark = pytest.mark.skipif(
    not os.environ.get("NEXUS_READ_REPLICA_URL"),
    reason="NEXUS_READ_REPLICA_URL not set — read replica E2E tests skipped",
)


@pytest.fixture(scope="module")
def record_store():
    """Create a SQLAlchemyRecordStore with read replica configured."""
    from nexus.storage.record_store import SQLAlchemyRecordStore

    store = SQLAlchemyRecordStore(
        db_url=os.environ["NEXUS_DATABASE_URL"],
        read_replica_url=os.environ["NEXUS_READ_REPLICA_URL"],
        create_tables=True,
    )
    yield store
    store.close()


class TestReadReplicaE2E:
    """End-to-end tests with real PostgreSQL primary + replica."""

    def test_reads_go_to_replica(self, record_store):
        """Verify read_engine is distinct from primary engine."""
        assert record_store.has_read_replica is True
        assert record_store.read_engine is not record_store.engine
        assert record_store.read_session_factory is not record_store.session_factory

    def test_writes_go_to_primary(self, record_store):
        """Verify write session uses primary engine."""
        session = record_store.session_factory()
        try:
            # Should be bound to primary engine
            bind = session.get_bind()
            assert bind is record_store.engine
        finally:
            session.close()

    def test_replica_down_errors_gracefully(self):
        """When replica URL is unreachable, operations should error gracefully."""
        from nexus.storage.record_store import SQLAlchemyRecordStore

        store = SQLAlchemyRecordStore(
            db_url=os.environ["NEXUS_DATABASE_URL"],
            read_replica_url="postgresql://fake:fake@unreachable-host:5432/nonexistent",
            create_tables=False,
        )
        try:
            assert store.has_read_replica is True
            # The read engine is created but connections will fail
            # This validates graceful failure, not silent corruption
            from sqlalchemy import text
            from sqlalchemy.exc import OperationalError

            with pytest.raises(OperationalError), store.read_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        finally:
            store.close()

    def test_rebac_check_uses_replica(self, record_store):
        """ReBAC TupleRepository read methods should use the read engine."""
        from nexus.bricks.rebac.tuples.repository import TupleRepository

        repo = TupleRepository(
            engine=record_store.engine,
            read_engine=record_store.read_engine,
        )
        assert repo.read_engine is record_store.read_engine

        # Zone revision read should not raise
        rev = repo.get_zone_revision("e2e-test-zone")
        assert isinstance(rev, int)
        assert rev >= 0
