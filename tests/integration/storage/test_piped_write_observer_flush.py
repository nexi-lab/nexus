"""Integration tests for the write-through RecordStoreWriteObserver (#4738).

Against a real SQLite RecordStore: ``on_write`` / ``on_delete`` / ``on_rename``
commit operation_log + file_paths + version_history before returning and
hand back the operation_log ``sequence_number`` (the projection sequence);
``flush_sync()`` drains whatever was queued without waiting and runs the
deferred MCL work inline; the deferred phase records MCL rows.
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.metadata import FileMetadata
from nexus.storage.models import (
    FilePathModel,
    MetadataChangeLogModel,
    OperationLogModel,
    VersionHistoryModel,
)
from nexus.storage.operation_logger import OperationLogger
from nexus.storage.piped_record_store_write_observer import (
    RecordStoreWriteObserver as ObserverWriteObserver,
)
from nexus.storage.record_store import SQLAlchemyRecordStore
from nexus.storage.record_store_write_observer import RecordStoreWriteObserver
from nexus.storage.version_recorder import version_gen


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def record_store(temp_dir: Path) -> Generator[SQLAlchemyRecordStore, None, None]:
    rs = SQLAlchemyRecordStore(db_path=temp_dir / "metadata.db")
    yield rs
    rs.close()


@pytest.fixture
def observer(record_store: SQLAlchemyRecordStore) -> Generator[ObserverWriteObserver, None, None]:
    obs = ObserverWriteObserver(record_store)
    yield obs
    obs.flush_sync()
    obs.cancel()


def _make_metadata(
    path: str = "/test.txt",
    *,
    content_id: str = "abc123",
    size: int = 100,
    version: int = 1,
    gen: int = 1,
) -> FileMetadata:
    return FileMetadata(
        path=path,
        size=size,
        content_id=content_id,
        mime_type="text/plain",
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
        version=version,
        gen=gen,
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


class TestWriteThrough:
    """Critical rows are committed when the intake call returns."""

    def test_on_write_is_visible_immediately_and_returns_op_log_seq(
        self, record_store: SQLAlchemyRecordStore, observer: ObserverWriteObserver
    ) -> None:
        seq = observer.on_write(
            _make_metadata("/prebuf.txt", content_id="h1"),
            is_new=True,
            path="/prebuf.txt",
            zone_id="root",
            agent_id="agent-1",
        )
        assert isinstance(seq, int)

        # No flush call in between — the rows are already there (acceptance #2).
        with record_store.session_factory() as session:
            ops = session.query(OperationLogModel).all()
            assert len(ops) == 1
            assert ops[0].path == "/prebuf.txt"
            assert ops[0].sequence_number == seq
            assert ops[0].agent_id == "agent-1"
            assert ops[0].delivered is False

            fps = session.query(FilePathModel).filter(FilePathModel.deleted_at.is_(None)).all()
            assert len(fps) == 1 and fps[0].virtual_path == "/prebuf.txt"

            vhs = session.query(VersionHistoryModel).all()
            assert len(vhs) == 1
            assert version_gen(vhs[0]) == 1, "kernel gen recorded for reconcile"

            applied, latest = OperationLogger(session).projection_state(seq, zone_id="root")
            assert applied is True and latest == seq

        assert observer.metrics["applied_seq"] == seq
        assert observer.metrics["pending_events"] == 0

    def test_sequences_increase_across_writes_and_versions_accumulate(
        self, record_store: SQLAlchemyRecordStore, observer: ObserverWriteObserver
    ) -> None:
        s1 = observer.on_write(
            _make_metadata("/multi.txt", content_id="v1"),
            is_new=True,
            path="/multi.txt",
            zone_id="root",
        )
        s2 = observer.on_write(
            _make_metadata("/multi.txt", content_id="v2", version=2, gen=2),
            is_new=False,
            path="/multi.txt",
            zone_id="root",
        )
        assert s1 is not None and s2 is not None and s2 > s1
        with record_store.session_factory() as session:
            vhs = (
                session.execute(
                    select(VersionHistoryModel).order_by(VersionHistoryModel.version_number)
                )
                .scalars()
                .all()
            )
            assert [v.content_id for v in vhs] == ["v1", "v2"]
            assert [version_gen(v) for v in vhs] == [1, 2]

    def test_delete_event_produces_correct_records(
        self, record_store: SQLAlchemyRecordStore, observer: ObserverWriteObserver
    ) -> None:
        metadata = _make_metadata("/del_test.txt", content_id="d1")
        observer.on_write(metadata, is_new=True, path="/del_test.txt", zone_id="root")
        seq = observer.on_delete(path="/del_test.txt", metadata=metadata, zone_id="root")
        assert isinstance(seq, int)

        with record_store.session_factory() as session:
            ops = session.query(OperationLogModel).order_by(OperationLogModel.sequence_number).all()
            assert [o.operation_type for o in ops] == ["write", "delete"]
            assert ops[0].change_type == "upsert"
            assert ops[1].change_type == "delete" and ops[1].entity_urn is not None
            assert ops[1].sequence_number == seq
            active = session.query(FilePathModel).filter(FilePathModel.deleted_at.is_(None)).all()
            assert active == []

    def test_rename_produces_two_operation_log_rows_and_returns_upsert_seq(
        self, record_store: SQLAlchemyRecordStore, observer: ObserverWriteObserver
    ) -> None:
        metadata = _make_metadata("/old_name.txt", content_id="r1")
        observer.on_write(metadata, is_new=True, path="/old_name.txt", zone_id="root")
        seq = observer.on_rename(
            old_path="/old_name.txt", new_path="/new_name.txt", metadata=metadata, zone_id="root"
        )

        with record_store.session_factory() as session:
            ops = session.query(OperationLogModel).order_by(OperationLogModel.sequence_number).all()
            # Write (1 row) + Rename (2 rows: delete old + upsert new)
            assert len(ops) == 3
            rename_ops = [o for o in ops if o.operation_type == "rename"]
            assert {o.change_type for o in rename_ops} == {"delete", "upsert"}
            assert seq == max(o.sequence_number for o in rename_ops)
            fp = session.query(FilePathModel).filter(FilePathModel.deleted_at.is_(None)).one()
            assert fp.virtual_path == "/new_name.txt"

    def test_mkdir_and_rmdir_events(
        self, record_store: SQLAlchemyRecordStore, observer: ObserverWriteObserver
    ) -> None:
        s1 = observer.on_mkdir(path="/test_dir", zone_id="root")
        s2 = observer.on_rmdir(path="/test_dir", zone_id="root", recursive=True)
        assert s1 is not None and s2 is not None and s2 > s1
        with record_store.session_factory() as session:
            ops = session.query(OperationLogModel).order_by(OperationLogModel.sequence_number).all()
            assert [o.operation_type for o in ops] == ["mkdir", "rmdir_recursive"]

    def test_write_event_has_entity_urn(
        self, record_store: SQLAlchemyRecordStore, observer: ObserverWriteObserver
    ) -> None:
        observer.on_write(
            _make_metadata("/urn_test.txt", content_id="u1"),
            is_new=True,
            path="/urn_test.txt",
            zone_id="root",
            agent_id="agent-1",
        )
        with record_store.session_factory() as session:
            ops = session.query(OperationLogModel).all()
            assert len(ops) == 1
            assert ops[0].entity_urn is not None and "root" in ops[0].entity_urn
            assert ops[0].aspect_name == "file_metadata"
            assert ops[0].change_type == "upsert"


class TestFlushSync:
    """flush_sync() drains queued events and deferred work inline."""

    def test_flush_sync_commits_queued_events(
        self,
        record_store: SQLAlchemyRecordStore,
        observer: ObserverWriteObserver,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Queue without waiting (what a non-strict timeout leaves behind),
        # with the commit thread disabled so flush_sync does the work.
        monkeypatch.setattr(observer, "_ensure_threads_locked", lambda: None)
        observer._enqueue(
            {
                "op": "write",
                "path": "/queued.txt",
                "is_new": True,
                "zone_id": "root",
                "agent_id": None,
                "snapshot_hash": None,
                "metadata_snapshot": None,
                "metadata": _make_metadata("/queued.txt", content_id="q1").to_dict(),
            }
        )
        assert observer.metrics["pending_events"] == 1
        assert observer.flush_sync() == 1
        assert observer.metrics["pending_events"] == 0
        assert observer.metrics["total_flushed"] == 1
        with record_store.session_factory() as session:
            assert session.query(VersionHistoryModel).count() == 1

    @pytest.mark.anyio
    async def test_flush_empty_returns_zero(self, record_store: SQLAlchemyRecordStore) -> None:
        observer = ObserverWriteObserver(record_store)
        assert await observer.flush() == 0
        observer.cancel()


class TestDeferredPhase:
    """MCL rows are recorded by the deferred phase, after the critical commit."""

    def test_flush_batch_sync_then_run_deferred_records_mcl(
        self, record_store: SQLAlchemyRecordStore, observer: ObserverWriteObserver
    ) -> None:
        event = {
            "op": "write",
            "path": "/mcl_test.txt",
            "is_new": True,
            "zone_id": "root",
            "agent_id": None,
            "snapshot_hash": None,
            "metadata_snapshot": None,
            "metadata": _make_metadata("/mcl_test.txt", content_id="m1").to_dict(),
        }
        observer._flush_batch_sync([event])
        assert event["projection_seq"] is not None, "critical commit stamps the sequence"
        with record_store.session_factory() as session:
            assert session.query(OperationLogModel).count() == 1
            assert session.query(MetadataChangeLogModel).count() == 0, "MCL is deferred"

        observer._run_deferred([event])
        with record_store.session_factory() as session:
            mcl = session.query(MetadataChangeLogModel).all()
            assert len(mcl) == 1
            assert mcl[0].change_type == "upsert" and "root" in mcl[0].entity_urn

    def test_on_write_then_flush_sync_lands_mcl(
        self, record_store: SQLAlchemyRecordStore, observer: ObserverWriteObserver
    ) -> None:
        observer.on_write(
            _make_metadata("/deferred.txt", content_id="dd"),
            is_new=True,
            path="/deferred.txt",
            zone_id="root",
        )
        observer.flush_sync()  # drains the deferred queue inline
        with record_store.session_factory() as session:
            assert session.query(MetadataChangeLogModel).count() == 1

    def test_debounce_parameter_kept_for_compat(self, record_store: SQLAlchemyRecordStore) -> None:
        observer_default = ObserverWriteObserver(record_store)
        assert observer_default._debounce == 0.2
        observer_custom = ObserverWriteObserver(record_store, debounce_seconds=0.5)
        assert observer_custom._debounce == 0.5
        observer_default.cancel()
        observer_custom.cancel()
