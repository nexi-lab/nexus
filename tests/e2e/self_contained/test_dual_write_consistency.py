"""Integration tests verifying Metastore AND RecordStore consistency.

After each operation (write, delete, rename, batch), both stores should
contain consistent data with matching fields.

Phase 1.4 of #1246/#1330 consolidation plan.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from nexus import CASLocalBackend
from nexus.core.config import ParseConfig, PermissionConfig
from nexus.factory import create_nexus_fs
from nexus.storage.models import FilePathModel, VersionHistoryModel
from nexus.storage.operation_logger import OperationLogger
from nexus.storage.record_store import SQLAlchemyRecordStore
from tests.testkit.auth import TEST_CONTEXT

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS


# Kernel-backed metastore (post-RaftMetadataStore-deletion) is always
# available because the redb engine ships in the nexus_runtime extension.
def _try_create_raft_store(path: str) -> str:
    """Return a redb path; ``create_nexus_fs`` opens the kernel-backed proxy."""
    return path


@pytest.fixture(scope="module")
def _dual_write_base(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("nexus_dual_write")


@pytest.fixture(scope="module")
def record_store(_dual_write_base: Path) -> Generator[SQLAlchemyRecordStore, None, None]:
    rs = SQLAlchemyRecordStore(db_path=_dual_write_base / "metadata.db")
    yield rs
    rs.close()


@pytest.fixture(scope="module")
async def nx(_dual_write_base: Path, record_store: SQLAlchemyRecordStore):
    metadata_store = _try_create_raft_store(str(_dual_write_base / "raft-metadata"))

    nx = create_nexus_fs(
        backend=CASLocalBackend(str(_dual_write_base / "data")),
        metadata_store=metadata_store,
        record_store=record_store,
        parsing=ParseConfig(auto_parse=False),
        permissions=PermissionConfig(enforce=False),
        init_cred=TEST_CONTEXT,
        enable_write_buffer=False,  # sync observer — tests query RecordStore immediately
    )
    yield nx
    nx.close()


# =========================================================================
# Write consistency
# =========================================================================


class TestWriteConsistency:
    """After write(), both Metastore and RecordStore should be consistent."""

    @pytest.mark.asyncio
    async def test_new_file_exists_in_both_stores(
        self, nx: NexusFS, record_store: SQLAlchemyRecordStore
    ) -> None:
        result = nx.write("/test.txt", b"hello world")

        # Metastore has the file
        meta = nx._kernel.sys_stat("/test.txt", "root")
        assert meta is not None
        assert meta["content_id"] == result["content_id"]
        assert meta["size"] == len(b"hello world")

        # RecordStore has the file
        with record_store.session_factory() as session:
            fp = (
                session.query(FilePathModel)
                .filter(
                    FilePathModel.virtual_path == "/test.txt",
                    FilePathModel.deleted_at.is_(None),
                )
                .one()
            )
            assert fp.content_id == result["content_id"]
            assert fp.size_bytes == len(b"hello world")
            assert fp.current_version == 1

    @pytest.mark.asyncio
    async def test_field_consistency_after_write(
        self, nx: NexusFS, record_store: SQLAlchemyRecordStore
    ) -> None:
        """All overlapping fields should match between Metastore and RecordStore."""
        nx.write("/consistent.txt", b"data")

        meta = nx._kernel.sys_stat("/consistent.txt", "root")
        assert meta is not None

        with record_store.session_factory() as session:
            fp = (
                session.query(FilePathModel)
                .filter(
                    FilePathModel.virtual_path == "/consistent.txt",
                    FilePathModel.deleted_at.is_(None),
                )
                .one()
            )

            # Field-by-field consistency check
            assert fp.virtual_path == meta["path"]
            assert fp.content_id == meta["content_id"]
            assert fp.size_bytes == meta["size"]
            assert meta["version"] == 1
            assert fp.current_version == 1

    @pytest.mark.asyncio
    async def test_update_version_consistency(
        self, nx: NexusFS, record_store: SQLAlchemyRecordStore
    ) -> None:
        """After update, version numbers should match across stores."""
        nx.write("/ver.txt", b"v1")
        nx.write("/ver.txt", b"v2")
        nx.write("/ver.txt", b"v3")

        meta = nx._kernel.sys_stat("/ver.txt", "root")
        assert meta is not None
        assert meta["version"] == 3

        with record_store.session_factory() as session:
            fp = (
                session.query(FilePathModel)
                .filter(
                    FilePathModel.virtual_path == "/ver.txt",
                    FilePathModel.deleted_at.is_(None),
                )
                .one()
            )
            # RecordStore independently tracks versions: 1 create + 2 updates = 3
            assert fp.current_version == 3

            # Should have 3 version history entries
            vhs = (
                session.query(VersionHistoryModel)
                .filter(
                    VersionHistoryModel.resource_id == fp.path_id,
                )
                .order_by(VersionHistoryModel.version_number)
                .all()
            )
            assert len(vhs) == 3
            assert [v.version_number for v in vhs] == [1, 2, 3]


# =========================================================================
# Delete consistency
# =========================================================================


class TestDeleteConsistency:
    """After delete(), Metastore entry is gone, RecordStore is soft-deleted."""

    @pytest.mark.asyncio
    async def test_delete_removes_from_metastore(
        self, nx: NexusFS, record_store: SQLAlchemyRecordStore
    ) -> None:
        nx.write("/del.txt", b"content")
        nx.sys_unlink("/del.txt")

        assert nx._kernel.sys_stat("/del.txt", "root") is None

    @pytest.mark.asyncio
    async def test_delete_soft_deletes_in_record_store(
        self, nx: NexusFS, record_store: SQLAlchemyRecordStore
    ) -> None:
        nx.write("/del.txt", b"content")
        nx.sys_unlink("/del.txt")

        with record_store.session_factory() as session:
            fp = (
                session.query(FilePathModel)
                .filter(
                    FilePathModel.virtual_path == "/del.txt",
                )
                .one()
            )
            assert fp.deleted_at is not None

    @pytest.mark.asyncio
    async def test_delete_audit_trail_exists(
        self, nx: NexusFS, record_store: SQLAlchemyRecordStore
    ) -> None:
        nx.write("/del.txt", b"content")
        nx.sys_unlink("/del.txt")

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

    @pytest.mark.asyncio
    async def test_rename_updates_metastore_path(
        self, nx: NexusFS, record_store: SQLAlchemyRecordStore
    ) -> None:
        nx.write("/old.txt", b"content")
        nx.sys_rename("/old.txt", "/new.txt")

        assert nx._kernel.sys_stat("/old.txt", "root") is None
        meta = nx._kernel.sys_stat("/new.txt", "root")
        assert meta is not None
        assert meta["path"] == "/new.txt"

    @pytest.mark.asyncio
    async def test_rename_audit_trail(
        self, nx: NexusFS, record_store: SQLAlchemyRecordStore
    ) -> None:
        nx.write("/old.txt", b"content")
        nx.sys_rename("/old.txt", "/new.txt")

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

    @pytest.mark.asyncio
    async def test_batch_all_files_in_both_stores(
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
            meta = nx._kernel.sys_stat(path, "root")
            assert meta is not None, f"{path} missing from Metastore"
            assert meta["size"] == len(content)

        # All files in RecordStore
        with record_store.session_factory() as session:
            fps = (
                session.query(FilePathModel)
                .filter(
                    FilePathModel.deleted_at.is_(None),
                )
                .all()
            )
            record_paths = {fp.virtual_path for fp in fps}
            for path, _ in files:
                assert path in record_paths, f"{path} missing from RecordStore"

    @pytest.mark.asyncio
    async def test_batch_operation_log_entries(
        self, nx: NexusFS, record_store: SQLAlchemyRecordStore
    ) -> None:
        files = [("/x.txt", b"x"), ("/y.txt", b"y")]
        nx.write_batch(files)

        with record_store.session_factory() as session:
            logger = OperationLogger(session)
            ops = logger.list_operations(operation_type="write")
            assert len(ops) >= 2
