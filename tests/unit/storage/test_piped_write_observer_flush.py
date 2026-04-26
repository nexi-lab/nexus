"""Tests for RecordStoreWriteObserver (OBSERVE-phase) flush behavior.

Ensures that flush_sync() drains pending events and commits them to the
database before returning, so that subsequent queries (e.g. list_versions)
see version history created by immediately preceding writes.

Also tests the debounce → flush path for the OBSERVE-phase observer.
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.metadata import FileMetadata
from nexus.storage.models import (
    FilePathModel,
    MetadataChangeLogModel,
    OperationLogModel,
    VersionHistoryModel,
)
from nexus.storage.piped_record_store_write_observer import (
    RecordStoreWriteObserver as ObserverWriteObserver,
)
from nexus.storage.record_store import SQLAlchemyRecordStore
from nexus.storage.record_store_write_observer import RecordStoreWriteObserver


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def record_store(temp_dir: Path) -> Generator[SQLAlchemyRecordStore, None, None]:
    rs = SQLAlchemyRecordStore(db_path=temp_dir / "metadata.db")
    yield rs
    rs.close()


def _make_metadata(
    path: str = "/test.txt",
    *,
    etag: str = "abc123",
    size: int = 100,
    version: int = 1,
) -> FileMetadata:
    return FileMetadata(
        path=path,
        size=size,
        etag=etag,
        mime_type="text/plain",
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
        version=version,
        zone_id=ROOT_ZONE_ID,
        owner_id="user1",
    )


class TestSyncObserverFlush:
    """RecordStoreWriteObserver.flush() is a no-op (commits inline)."""

    @pytest.mark.anyio
    async def test_flush_returns_zero(self, record_store: SQLAlchemyRecordStore) -> None:
        observer = RecordStoreWriteObserver(record_store)
        result = await observer.flush()
        assert result == 0


class TestObserverFlushSync:
    """flush_sync() on the OBSERVE-phase observer drains pending events."""

    @pytest.mark.anyio
    async def test_flush_sync_commits_to_db(self, record_store: SQLAlchemyRecordStore) -> None:
        observer = ObserverWriteObserver(record_store, debounce_seconds=10.0)

        # Manually populate pending events (simulating on_mutation path)
        metadata = _make_metadata("/prebuf.txt", etag="h1")
        event = {
            "op": "write",
            "path": "/prebuf.txt",
            "is_new": True,
            "zone_id": "root",
            "agent_id": None,
            "snapshot_hash": None,
            "metadata_snapshot": None,
            "metadata": metadata.to_dict(),
        }
        observer._pending.append(event)

        assert len(observer._pending) == 1

        # flush_sync should commit directly to DB
        flushed = observer.flush_sync()
        assert flushed == 1
        assert len(observer._pending) == 0

        # Verify data is in the database
        with record_store.session_factory() as session:
            ops = session.query(OperationLogModel).all()
            assert len(ops) == 1
            assert ops[0].path == "/prebuf.txt"

            fps = session.query(FilePathModel).filter(FilePathModel.deleted_at.is_(None)).all()
            assert len(fps) == 1
            assert fps[0].virtual_path == "/prebuf.txt"

            vhs = session.query(VersionHistoryModel).all()
            assert len(vhs) == 1

        observer.cancel()

    @pytest.mark.anyio
    async def test_flush_empty_returns_zero(self, record_store: SQLAlchemyRecordStore) -> None:
        observer = ObserverWriteObserver(record_store)
        flushed = await observer.flush()
        assert flushed == 0
        observer.cancel()

    @pytest.mark.anyio
    async def test_flush_handles_multiple_ops(self, record_store: SQLAlchemyRecordStore) -> None:
        observer = ObserverWriteObserver(record_store, debounce_seconds=10.0)

        # Write, then update — populate pending directly
        m1 = _make_metadata("/multi.txt", etag="v1")
        e1 = {
            "op": "write",
            "path": "/multi.txt",
            "is_new": True,
            "zone_id": "root",
            "agent_id": None,
            "snapshot_hash": None,
            "metadata_snapshot": None,
            "metadata": m1.to_dict(),
        }
        observer._pending.append(e1)

        m2 = _make_metadata("/multi.txt", etag="v2", version=2)
        e2 = {
            "op": "write",
            "path": "/multi.txt",
            "is_new": False,
            "zone_id": "root",
            "agent_id": None,
            "snapshot_hash": None,
            "metadata_snapshot": None,
            "metadata": m2.to_dict(),
        }
        observer._pending.append(e2)

        assert len(observer._pending) == 2

        flushed = observer.flush_sync()
        assert flushed == 2

        with record_store.session_factory() as session:
            vhs = session.query(VersionHistoryModel).all()
            assert len(vhs) == 2

        observer.cancel()


class TestObserverFlushMetrics:
    """flush_sync() updates observer metrics correctly."""

    @pytest.mark.anyio
    async def test_flush_increments_total_flushed(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        observer = ObserverWriteObserver(record_store, debounce_seconds=10.0)
        metadata = _make_metadata("/metrics.txt", etag="mh1")
        event = {
            "op": "write",
            "path": "/metrics.txt",
            "is_new": True,
            "zone_id": "root",
            "agent_id": None,
            "snapshot_hash": None,
            "metadata_snapshot": None,
            "metadata": metadata.to_dict(),
        }
        observer._pending.append(event)

        assert observer.metrics["total_flushed"] == 0

        observer.flush_sync()

        assert observer.metrics["total_flushed"] == 1
        observer.cancel()


class TestObserverSQLiteIntegration:
    """Verify OBSERVE-phase observer produces correct records on SQLite.

    _process_events_in_session() must produce complete records:
    entity_urn, aspect_name, change_type, rename two-URN pattern,
    delete soft-delete.
    """

    @pytest.mark.anyio
    async def test_write_event_has_entity_urn(self, record_store: SQLAlchemyRecordStore) -> None:
        """Write event must populate entity_urn, aspect_name, change_type."""
        observer = ObserverWriteObserver(record_store, debounce_seconds=10.0)
        metadata = _make_metadata("/urn_test.txt", etag="u1")
        event = {
            "op": "write",
            "path": "/urn_test.txt",
            "is_new": True,
            "zone_id": "root",
            "agent_id": "agent-1",
            "snapshot_hash": None,
            "metadata_snapshot": None,
            "metadata": metadata.to_dict(),
        }
        observer._pending.append(event)

        flushed = observer.flush_sync()
        assert flushed == 1

        with record_store.session_factory() as session:
            ops = session.query(OperationLogModel).all()
            assert len(ops) == 1
            assert ops[0].entity_urn is not None
            assert "root" in ops[0].entity_urn
            assert ops[0].aspect_name == "file_metadata"
            assert ops[0].change_type == "upsert"
            assert ops[0].agent_id == "agent-1"
            assert ops[0].delivered is False

        observer.cancel()

    @pytest.mark.anyio
    async def test_delete_event_produces_correct_records(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """Delete event must have entity_urn, change_type='delete'."""
        observer = ObserverWriteObserver(record_store, debounce_seconds=10.0)
        metadata = _make_metadata("/del_test.txt", etag="d1")

        # First write the file so there's something to delete
        write_event = {
            "op": "write",
            "path": "/del_test.txt",
            "is_new": True,
            "zone_id": "root",
            "agent_id": None,
            "snapshot_hash": None,
            "metadata_snapshot": None,
            "metadata": metadata.to_dict(),
        }
        observer._pending.append(write_event)

        delete_event = {
            "op": "delete",
            "path": "/del_test.txt",
            "zone_id": "root",
            "agent_id": None,
            "snapshot_hash": "d1",
            "metadata_snapshot": metadata.to_dict(),
        }
        observer._pending.append(delete_event)

        flushed = observer.flush_sync()
        assert flushed == 2

        with record_store.session_factory() as session:
            ops = session.query(OperationLogModel).order_by(OperationLogModel.created_at).all()
            assert len(ops) == 2
            # Write op
            assert ops[0].operation_type == "write"
            assert ops[0].change_type == "upsert"
            # Delete op
            assert ops[1].operation_type == "delete"
            assert ops[1].entity_urn is not None
            assert ops[1].change_type == "delete"

        observer.cancel()

    @pytest.mark.anyio
    async def test_rename_produces_two_operation_log_rows(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        """Rename must produce two operation_log rows: DELETE old + UPSERT new."""
        observer = ObserverWriteObserver(record_store, debounce_seconds=10.0)
        metadata = _make_metadata("/old_name.txt", etag="r1")

        # Write the file first
        write_event = {
            "op": "write",
            "path": "/old_name.txt",
            "is_new": True,
            "zone_id": "root",
            "agent_id": None,
            "snapshot_hash": None,
            "metadata_snapshot": None,
            "metadata": metadata.to_dict(),
        }
        observer._pending.append(write_event)

        rename_event = {
            "op": "rename",
            "path": "/old_name.txt",
            "new_path": "/new_name.txt",
            "zone_id": "root",
            "agent_id": None,
            "snapshot_hash": "r1",
            "metadata_snapshot": metadata.to_dict(),
        }
        observer._pending.append(rename_event)

        flushed = observer.flush_sync()
        assert flushed == 2

        with record_store.session_factory() as session:
            ops = session.query(OperationLogModel).order_by(OperationLogModel.created_at).all()
            # Write (1 row) + Rename (2 rows: delete old + upsert new)
            assert len(ops) == 3
            rename_ops = [o for o in ops if o.operation_type == "rename"]
            assert len(rename_ops) == 2
            change_types = {o.change_type for o in rename_ops}
            assert change_types == {"delete", "upsert"}

        observer.cancel()

    @pytest.mark.anyio
    async def test_mkdir_and_rmdir_events(self, record_store: SQLAlchemyRecordStore) -> None:
        """mkdir and rmdir events should be recorded without entity_urn."""
        observer = ObserverWriteObserver(record_store, debounce_seconds=10.0)

        mkdir_event = {
            "op": "mkdir",
            "path": "/test_dir",
            "zone_id": "root",
            "agent_id": None,
        }
        rmdir_event = {
            "op": "rmdir",
            "path": "/test_dir",
            "zone_id": "root",
            "agent_id": None,
            "recursive": True,
        }
        observer._pending.append(mkdir_event)
        observer._pending.append(rmdir_event)

        flushed = observer.flush_sync()
        assert flushed == 2

        with record_store.session_factory() as session:
            ops = session.query(OperationLogModel).order_by(OperationLogModel.created_at).all()
            assert len(ops) == 2
            assert ops[0].operation_type == "mkdir"
            assert ops[1].operation_type == "rmdir_recursive"

        observer.cancel()

    @pytest.mark.anyio
    async def test_batch_flush_records_mcl(self, record_store: SQLAlchemyRecordStore) -> None:
        """Batch flush path (via _flush_batch_sync) should record MCL entries."""
        observer = ObserverWriteObserver(record_store)
        metadata = _make_metadata("/mcl_test.txt", etag="m1")
        event = {
            "op": "write",
            "path": "/mcl_test.txt",
            "is_new": True,
            "zone_id": "root",
            "agent_id": None,
            "snapshot_hash": None,
            "metadata_snapshot": None,
            "metadata": metadata.to_dict(),
        }

        # Use _flush_batch_sync directly (the flush path)
        observer._flush_batch_sync([event])

        with record_store.session_factory() as session:
            # Phase 1: operation_log + version_history
            ops = session.query(OperationLogModel).all()
            assert len(ops) == 1
            assert ops[0].entity_urn is not None
            assert ops[0].aspect_name == "file_metadata"

            # Phase 2: MCL recording
            mcl = session.query(MetadataChangeLogModel).all()
            assert len(mcl) == 1
            assert mcl[0].change_type == "upsert"
            assert "root" in mcl[0].entity_urn

        observer.cancel()

    @pytest.mark.anyio
    async def test_debounce_parameter_defaults(self, record_store: SQLAlchemyRecordStore) -> None:
        """Verify debounce_seconds parameter is configurable and has correct default."""
        observer_default = ObserverWriteObserver(record_store)
        assert observer_default._debounce == 0.2

        observer_custom = ObserverWriteObserver(record_store, debounce_seconds=0.5)
        assert observer_custom._debounce == 0.5

        observer_zero = ObserverWriteObserver(record_store, debounce_seconds=0)
        assert observer_zero._debounce == 0

        observer_default.cancel()
        observer_custom.cancel()
        observer_zero.cancel()
