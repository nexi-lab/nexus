"""Integration tests verifying Metastore AND RecordStore consistency.

After each operation (write, delete, rename, batch), both stores should
contain consistent data with matching fields.

Phase 1.4 of #1246/#1330 consolidation plan.
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from nexus import LocalBackend, NexusFS
from nexus.factory import create_nexus_fs
from nexus.storage.models import FilePathModel, VersionHistoryModel
from nexus.storage.operation_logger import OperationLogger
from nexus.storage.record_store import SQLAlchemyRecordStore

# Try to import RaftMetadataStore — skip if native module unavailable
try:
    from nexus.storage.raft_metadata_store import RaftMetadataStore

    _raft_available = True
except Exception:
    _raft_available = False


def _try_create_raft_store(path: str) -> object | None:
    """Try to create a RaftMetadataStore; return None if native module unavailable."""
    if not _raft_available:
        return None
    try:
        return RaftMetadataStore.embedded(path)
    except RuntimeError:
        return None


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
def nx(
    temp_dir: Path, record_store: SQLAlchemyRecordStore
) -> Generator[NexusFS, None, None]:
    raft_store = _try_create_raft_store(str(temp_dir / "raft-metadata"))
    if raft_store is None:
        # Fallback to InMemoryFileMetadataStore with factory-style wiring
        from nexus.storage.record_store_syncer import RecordStoreSyncer
        from tests.helpers.in_memory_metadata_store import InMemoryFileMetadataStore

        metadata_store = InMemoryFileMetadataStore()
        write_observer = RecordStoreSyncer(record_store.session_factory)

        nx = NexusFS(
            backend=LocalBackend(str(temp_dir / "data")),
            metadata_store=metadata_store,
            record_store=record_store,
            enforce_permissions=False,
            auto_parse=False,
            write_observer=write_observer,
        )
    else:
        nx = create_nexus_fs(
            backend=LocalBackend(str(temp_dir / "data")),
            metadata_store=raft_store,
            record_store=record_store,
            auto_parse=False,
            enforce_permissions=False,
        )
    yield nx
    nx.close()


# =========================================================================
# Write consistency
# =========================================================================


class TestWriteConsistency:
    """After write(), both Metastore and RecordStore should be consistent."""

    def test_new_file_exists_in_both_stores(
        self, nx: NexusFS, record_store: SQLAlchemyRecordStore
    ) -> None:
        result = nx.write("/test.txt", b"hello world")

        # Metastore has the file
        meta = nx.metadata.get("/test.txt")
        assert meta is not None
        assert meta.etag == result["etag"]
        assert meta.size == len(b"hello world")

        # RecordStore has the file
        with record_store.session_factory() as session:
            fp = session.query(FilePathModel).filter(
                FilePathModel.virtual_path == "/test.txt",
                FilePathModel.deleted_at.is_(None),
            ).one()
            assert fp.content_hash == result["etag"]
            assert fp.size_bytes == len(b"hello world")
            assert fp.current_version == 1

    def test_field_consistency_after_write(
        self, nx: NexusFS, record_store: SQLAlchemyRecordStore
    ) -> None:
        """All overlapping fields should match between Metastore and RecordStore."""
        nx.write("/consistent.txt", b"data")

        meta = nx.metadata.get("/consistent.txt")
        assert meta is not None

        with record_store.session_factory() as session:
            fp = session.query(FilePathModel).filter(
                FilePathModel.virtual_path == "/consistent.txt",
                FilePathModel.deleted_at.is_(None),
            ).one()

            # Field-by-field consistency check
            assert fp.virtual_path == meta.path
            assert fp.content_hash == meta.etag
            assert fp.size_bytes == meta.size
            assert fp.current_version == meta.version

    def test_update_version_consistency(
        self, nx: NexusFS, record_store: SQLAlchemyRecordStore
    ) -> None:
        """After update, version numbers should match across stores."""
        nx.write("/ver.txt", b"v1")
        nx.write("/ver.txt", b"v2")
        nx.write("/ver.txt", b"v3")

        meta = nx.metadata.get("/ver.txt")
        assert meta is not None
        assert meta.version == 3

        with record_store.session_factory() as session:
            fp = session.query(FilePathModel).filter(
                FilePathModel.virtual_path == "/ver.txt",
                FilePathModel.deleted_at.is_(None),
            ).one()
            assert fp.current_version == 3

            # Should have 3 version history entries
            vhs = session.query(VersionHistoryModel).filter(
                VersionHistoryModel.resource_id == fp.path_id,
            ).order_by(VersionHistoryModel.version_number).all()
            assert len(vhs) == 3
            assert [v.version_number for v in vhs] == [1, 2, 3]


# =========================================================================
# Delete consistency
# =========================================================================


class TestDeleteConsistency:
    """After delete(), Metastore entry is gone, RecordStore is soft-deleted."""

    def test_delete_removes_from_metastore(
        self, nx: NexusFS, record_store: SQLAlchemyRecordStore
    ) -> None:
        nx.write("/del.txt", b"content")
        nx.delete("/del.txt")

        assert nx.metadata.get("/del.txt") is None

    def test_delete_soft_deletes_in_record_store(
        self, nx: NexusFS, record_store: SQLAlchemyRecordStore
    ) -> None:
        nx.write("/del.txt", b"content")
        nx.delete("/del.txt")

        with record_store.session_factory() as session:
            fp = session.query(FilePathModel).filter(
                FilePathModel.virtual_path == "/del.txt",
            ).one()
            assert fp.deleted_at is not None

    def test_delete_audit_trail_exists(
        self, nx: NexusFS, record_store: SQLAlchemyRecordStore
    ) -> None:
        nx.write("/del.txt", b"content")
        nx.delete("/del.txt")

        with record_store.session_factory() as session:
            logger = OperationLogger(session)
            ops = logger.list_operations(path="/del.txt")
            op_types = [op.operation_type for op in ops]
            assert "write" in op_types
            assert "delete" in op_types


# =========================================================================
# Rename consistency
# =========================================================================


class TestRenameConsistency:
    """After rename(), Metastore reflects new path, RecordStore has audit trail."""

    def test_rename_updates_metastore_path(
        self, nx: NexusFS, record_store: SQLAlchemyRecordStore
    ) -> None:
        nx.write("/old.txt", b"content")
        nx.rename("/old.txt", "/new.txt")

        assert nx.metadata.get("/old.txt") is None
        meta = nx.metadata.get("/new.txt")
        assert meta is not None
        assert meta.path == "/new.txt"

    def test_rename_audit_trail(
        self, nx: NexusFS, record_store: SQLAlchemyRecordStore
    ) -> None:
        nx.write("/old.txt", b"content")
        nx.rename("/old.txt", "/new.txt")

        with record_store.session_factory() as session:
            logger = OperationLogger(session)
            ops = logger.list_operations(path="/old.txt")
            rename_ops = [op for op in ops if op.operation_type == "rename"]
            assert len(rename_ops) == 1
            assert rename_ops[0].new_path == "/new.txt"


# =========================================================================
# Batch write consistency
# =========================================================================


class TestBatchWriteConsistency:
    """After write_batch(), all files exist in both stores."""

    def test_batch_all_files_in_both_stores(
        self, nx: NexusFS, record_store: SQLAlchemyRecordStore
    ) -> None:
        files = [
            ("/batch_a.txt", b"aaa"),
            ("/batch_b.txt", b"bbb"),
            ("/batch_c.txt", b"ccc"),
        ]
        results = nx.write_batch(files)
        assert len(results) == 3

        # All files in Metastore
        for path, content in files:
            meta = nx.metadata.get(path)
            assert meta is not None, f"{path} missing from Metastore"
            assert meta.size == len(content)

        # All files in RecordStore
        with record_store.session_factory() as session:
            fps = session.query(FilePathModel).filter(
                FilePathModel.deleted_at.is_(None),
            ).all()
            record_paths = {fp.virtual_path for fp in fps}
            for path, _ in files:
                assert path in record_paths, f"{path} missing from RecordStore"

    def test_batch_operation_log_entries(
        self, nx: NexusFS, record_store: SQLAlchemyRecordStore
    ) -> None:
        files = [("/x.txt", b"x"), ("/y.txt", b"y")]
        nx.write_batch(files)

        with record_store.session_factory() as session:
            logger = OperationLogger(session)
            ops = logger.list_operations(operation_type="write")
            assert len(ops) >= 2
