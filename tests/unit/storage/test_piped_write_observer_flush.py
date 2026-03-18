"""Tests for PipedRecordStoreWriteObserver.flush() — race condition fix.

Ensures that flush() drains the DT_PIPE and commits pending events to the
database before returning, so that subsequent queries (e.g. list_versions)
see version history created by immediately preceding writes.

Also tests the pre-buffer sync flush path (before pipe is ready).
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus.contracts.metadata import FileMetadata
from nexus.storage.models import FilePathModel, OperationLogModel, VersionHistoryModel
from nexus.storage.piped_record_store_write_observer import PipedRecordStoreWriteObserver
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
        backend_name="local",
        physical_path=etag,
        size=size,
        etag=etag,
        mime_type="text/plain",
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
        version=version,
        zone_id="root",
        created_by="test_user",
        owner_id="user1",
    )


class TestSyncObserverFlush:
    """RecordStoreWriteObserver.flush() is a no-op (commits inline)."""

    @pytest.mark.anyio
    async def test_flush_returns_zero(self, record_store: SQLAlchemyRecordStore) -> None:
        observer = RecordStoreWriteObserver(record_store)
        result = await observer.flush()
        assert result == 0


class TestPipedObserverPreBufferFlush:
    """flush() on piped observer before pipe is ready drains pre-buffer directly."""

    @pytest.mark.anyio
    async def test_flush_pre_buffer_commits_to_db(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        observer = PipedRecordStoreWriteObserver(record_store)
        # Pipe is NOT ready — events go to pre-buffer

        metadata = _make_metadata("/prebuf.txt", etag="h1")
        observer.on_write(metadata, is_new=True, path="/prebuf.txt", zone_id="root")

        # Pre-buffer should have one event
        assert len(observer._pre_buffer) == 1

        # Flush should commit directly to DB
        flushed = await observer.flush()
        assert flushed == 1
        assert len(observer._pre_buffer) == 0

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

    @pytest.mark.anyio
    async def test_flush_empty_pre_buffer_returns_zero(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        observer = PipedRecordStoreWriteObserver(record_store)
        flushed = await observer.flush()
        assert flushed == 0

    @pytest.mark.anyio
    async def test_flush_pre_buffer_handles_multiple_ops(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        observer = PipedRecordStoreWriteObserver(record_store)

        # Write, then update
        m1 = _make_metadata("/multi.txt", etag="v1")
        observer.on_write(m1, is_new=True, path="/multi.txt", zone_id="root")

        m2 = _make_metadata("/multi.txt", etag="v2", version=2)
        observer.on_write(m2, is_new=False, path="/multi.txt", zone_id="root")

        assert len(observer._pre_buffer) == 2

        flushed = await observer.flush()
        assert flushed == 2

        with record_store.session_factory() as session:
            vhs = session.query(VersionHistoryModel).all()
            assert len(vhs) == 2


class TestPipedObserverPipeFlush:
    """flush() on piped observer with active pipe drains pipe events."""

    @pytest.mark.anyio
    async def test_flush_drains_pipe_events(self, record_store: SQLAlchemyRecordStore) -> None:
        observer = PipedRecordStoreWriteObserver(record_store)

        # Simulate pipe being ready
        metadata = _make_metadata("/piped.txt", etag="ph1")
        event = {
            "op": "write",
            "path": "/piped.txt",
            "is_new": True,
            "zone_id": "root",
            "agent_id": None,
            "snapshot_hash": None,
            "metadata_snapshot": None,
            "metadata": metadata.to_dict(),
        }

        mock_pm = MagicMock()
        observer._pipe_manager = mock_pm
        observer._pipe_ready = True

        # pipe_read returns one event then raises PipeEmptyError
        from nexus.core.pipe import PipeEmptyError

        call_count = 0

        async def mock_pipe_read(path, blocking=True):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return json.dumps(event).encode()
            raise PipeEmptyError(path)

        mock_pm.pipe_read = mock_pipe_read

        flushed = await observer.flush()
        assert flushed == 1

        # Verify DB commit
        with record_store.session_factory() as session:
            ops = session.query(OperationLogModel).all()
            assert len(ops) == 1
            assert ops[0].path == "/piped.txt"

    @pytest.mark.anyio
    async def test_flush_empty_pipe_returns_zero(self, record_store: SQLAlchemyRecordStore) -> None:
        observer = PipedRecordStoreWriteObserver(record_store)

        mock_pm = MagicMock()
        observer._pipe_manager = mock_pm
        observer._pipe_ready = True

        from nexus.core.pipe import PipeEmptyError

        async def mock_pipe_read(path, blocking=True):
            raise PipeEmptyError(path)

        mock_pm.pipe_read = mock_pipe_read

        flushed = await observer.flush()
        assert flushed == 0


class TestPipedObserverFlushMetrics:
    """flush() updates observer metrics correctly."""

    @pytest.mark.anyio
    async def test_flush_increments_total_flushed(
        self, record_store: SQLAlchemyRecordStore
    ) -> None:
        observer = PipedRecordStoreWriteObserver(record_store)
        metadata = _make_metadata("/metrics.txt", etag="mh1")
        observer.on_write(metadata, is_new=True, path="/metrics.txt", zone_id="root")

        assert observer.metrics["total_flushed"] == 0

        await observer.flush()

        assert observer.metrics["total_flushed"] == 1
