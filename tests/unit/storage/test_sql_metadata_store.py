"""Unit tests for SqlMetadataStore — PostgreSQL as SSOT for file metadata.

Phase 4.1 of #1246/#1330 consolidation plan.
Tests all FileMetadataProtocol methods using SQLite-backed SQLAlchemy.
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nexus.core._metadata_generated import DT_DIR, DT_REG, FileMetadata
from nexus.storage.models import FilePathModel, OperationLogModel, VersionHistoryModel
from nexus.storage.record_store import SQLAlchemyRecordStore
from nexus.storage.sql_metadata_store import SqlMetadataStore


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
def store(record_store: SQLAlchemyRecordStore) -> SqlMetadataStore:
    return SqlMetadataStore(record_store.session_factory)


def _make_metadata(
    path: str = "/test.txt",
    *,
    etag: str = "abc123",
    size: int = 100,
    version: int = 1,
    zone_id: str = "default",
    owner_id: str | None = "user1",
    is_directory: bool = False,
    created_by: str | None = "test_user",
) -> FileMetadata:
    return FileMetadata(
        path=path,
        backend_name="local",
        physical_path=etag,
        size=size,
        etag=etag,
        mime_type=None if is_directory else "text/plain",
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
        version=version,
        zone_id=zone_id,
        created_by=created_by,
        owner_id=owner_id,
        entry_type=DT_DIR if is_directory else DT_REG,
    )


# =========================================================================
# Core CRUD
# =========================================================================


class TestGet:
    def test_get_nonexistent_returns_none(self, store: SqlMetadataStore) -> None:
        assert store.get("/nope") is None

    def test_get_after_put(self, store: SqlMetadataStore) -> None:
        meta = _make_metadata("/hello.txt", etag="h1", size=42)
        store.put(meta)

        result = store.get("/hello.txt")
        assert result is not None
        assert result.path == "/hello.txt"
        assert result.etag == "h1"
        assert result.size == 42
        assert result.backend_name == "local"
        assert result.version == 1
        assert result.zone_id == "default"
        assert result.owner_id == "user1"

    def test_get_directory(self, store: SqlMetadataStore) -> None:
        meta = _make_metadata("/mydir", is_directory=True)
        store.put(meta)

        result = store.get("/mydir")
        assert result is not None
        assert result.is_dir is True
        assert result.mime_type is None


class TestPut:
    def test_put_new_file(
        self, store: SqlMetadataStore, record_store: SQLAlchemyRecordStore
    ) -> None:
        meta = _make_metadata("/new.txt", etag="hash1")
        store.put(meta)

        # Verify FilePathModel
        with record_store.session_factory() as session:
            row = session.query(FilePathModel).filter_by(virtual_path="/new.txt").first()
            assert row is not None
            assert row.content_hash == "hash1"
            assert row.current_version == 1
            assert row.deleted_at is None

    def test_put_creates_version_history(
        self, store: SqlMetadataStore, record_store: SQLAlchemyRecordStore
    ) -> None:
        meta = _make_metadata("/ver.txt", etag="h1")
        store.put(meta)

        with record_store.session_factory() as session:
            versions = session.query(VersionHistoryModel).filter_by(resource_type="file").all()
            assert len(versions) == 1
            assert versions[0].version_number == 1
            assert versions[0].content_hash == "h1"

    def test_put_creates_operation_log(
        self, store: SqlMetadataStore, record_store: SQLAlchemyRecordStore
    ) -> None:
        meta = _make_metadata("/logged.txt", etag="h2")
        store.put(meta)

        with record_store.session_factory() as session:
            ops = session.query(OperationLogModel).filter_by(path="/logged.txt").all()
            assert len(ops) == 1
            assert ops[0].operation_type == "write"
            assert ops[0].status == "success"

    def test_put_update_increments_version(
        self, store: SqlMetadataStore, record_store: SQLAlchemyRecordStore
    ) -> None:
        store.put(_make_metadata("/upd.txt", etag="v1"))
        store.put(_make_metadata("/upd.txt", etag="v2"))

        result = store.get("/upd.txt")
        assert result is not None
        assert result.etag == "v2"
        assert result.version == 2

        # Two version history entries
        with record_store.session_factory() as session:
            versions = (
                session.query(VersionHistoryModel)
                .filter_by(resource_type="file")
                .order_by(VersionHistoryModel.version_number)
                .all()
            )
            assert len(versions) == 2
            assert versions[0].version_number == 1
            assert versions[1].version_number == 2
            # Lineage: v2 points to v1
            assert versions[1].parent_version_id == versions[0].version_id

    def test_put_metadata_only_no_etag(self, store: SqlMetadataStore) -> None:
        """Update without etag should not create version history."""
        store.put(_make_metadata("/noetag.txt", etag="first"))
        store.put(
            FileMetadata(
                path="/noetag.txt",
                backend_name="local",
                physical_path="/noetag.txt",
                size=50,
                etag=None,
                version=1,
            )
        )
        result = store.get("/noetag.txt")
        assert result is not None
        assert result.etag is None
        assert result.size == 50

    def test_put_reuses_soft_deleted_path(self, store: SqlMetadataStore) -> None:
        store.put(_make_metadata("/reuse.txt", etag="old"))
        store.delete("/reuse.txt")
        store.put(_make_metadata("/reuse.txt", etag="new"))

        result = store.get("/reuse.txt")
        assert result is not None
        assert result.etag == "new"
        assert result.version == 1  # New file, version starts at 1


class TestDelete:
    def test_delete_nonexistent_returns_none(self, store: SqlMetadataStore) -> None:
        assert store.delete("/nope") is None

    def test_delete_existing(self, store: SqlMetadataStore) -> None:
        store.put(_make_metadata("/del.txt", etag="d1", size=77))
        result = store.delete("/del.txt")
        assert result is not None
        assert result["path"] == "/del.txt"
        assert result["size"] == 77
        assert result["etag"] == "d1"

        # No longer visible
        assert store.get("/del.txt") is None
        assert store.exists("/del.txt") is False

    def test_delete_logs_operation(
        self, store: SqlMetadataStore, record_store: SQLAlchemyRecordStore
    ) -> None:
        store.put(_make_metadata("/dlog.txt", etag="x"))
        store.delete("/dlog.txt")

        with record_store.session_factory() as session:
            ops = (
                session.query(OperationLogModel)
                .filter_by(path="/dlog.txt", operation_type="delete")
                .all()
            )
            assert len(ops) == 1


class TestExists:
    def test_exists_false(self, store: SqlMetadataStore) -> None:
        assert store.exists("/nope") is False

    def test_exists_true(self, store: SqlMetadataStore) -> None:
        store.put(_make_metadata("/yes.txt"))
        assert store.exists("/yes.txt") is True

    def test_exists_after_delete(self, store: SqlMetadataStore) -> None:
        store.put(_make_metadata("/gone.txt"))
        store.delete("/gone.txt")
        assert store.exists("/gone.txt") is False


# =========================================================================
# List
# =========================================================================


class TestList:
    def test_list_empty(self, store: SqlMetadataStore) -> None:
        assert store.list() == []

    def test_list_all(self, store: SqlMetadataStore) -> None:
        store.put(_make_metadata("/a.txt", etag="a"))
        store.put(_make_metadata("/b.txt", etag="b"))
        result = store.list()
        assert len(result) == 2
        assert result[0].path == "/a.txt"
        assert result[1].path == "/b.txt"

    def test_list_with_prefix(self, store: SqlMetadataStore) -> None:
        store.put(_make_metadata("/docs/readme.md", etag="d1"))
        store.put(_make_metadata("/docs/guide.md", etag="d2"))
        store.put(_make_metadata("/src/main.py", etag="s1"))

        docs = store.list(prefix="/docs/")
        assert len(docs) == 2
        paths = [d.path for d in docs]
        assert "/docs/guide.md" in paths
        assert "/docs/readme.md" in paths

    def test_list_non_recursive(self, store: SqlMetadataStore) -> None:
        store.put(_make_metadata("/root/a.txt", etag="a"))
        store.put(_make_metadata("/root/sub/b.txt", etag="b"))

        result = store.list(prefix="/root/", recursive=False)
        assert len(result) == 1
        assert result[0].path == "/root/a.txt"

    def test_list_excludes_deleted(self, store: SqlMetadataStore) -> None:
        store.put(_make_metadata("/alive.txt", etag="a"))
        store.put(_make_metadata("/dead.txt", etag="d"))
        store.delete("/dead.txt")

        result = store.list()
        assert len(result) == 1
        assert result[0].path == "/alive.txt"

    def test_list_with_zone_id(self, store: SqlMetadataStore) -> None:
        store.put(_make_metadata("/z1.txt", etag="z1", zone_id="zone_a"))
        store.put(_make_metadata("/z2.txt", etag="z2", zone_id="zone_b"))

        result = store.list(zone_id="zone_a")
        assert len(result) == 1
        assert result[0].path == "/z1.txt"


class TestListPaginated:
    def test_paginate_basic(self, store: SqlMetadataStore) -> None:
        for i in range(5):
            store.put(_make_metadata(f"/p{i:02d}.txt", etag=f"e{i}"))

        page1 = store.list_paginated(limit=2)
        assert len(page1.items) == 2
        assert page1.has_more is True
        assert page1.next_cursor is not None

        page2 = store.list_paginated(limit=2, cursor=page1.next_cursor)
        assert len(page2.items) == 2
        assert page2.has_more is True

        page3 = store.list_paginated(limit=2, cursor=page2.next_cursor)
        assert len(page3.items) == 1
        assert page3.has_more is False


# =========================================================================
# Batch Operations
# =========================================================================


class TestBatch:
    def test_get_batch(self, store: SqlMetadataStore) -> None:
        store.put(_make_metadata("/a.txt", etag="a"))
        store.put(_make_metadata("/b.txt", etag="b"))

        result = store.get_batch(["/a.txt", "/b.txt", "/c.txt"])
        assert result["/a.txt"] is not None
        assert result["/b.txt"] is not None
        assert result["/c.txt"] is None

    def test_put_batch(self, store: SqlMetadataStore) -> None:
        items = [
            _make_metadata("/batch/1.txt", etag="b1"),
            _make_metadata("/batch/2.txt", etag="b2"),
            _make_metadata("/batch/3.txt", etag="b3"),
        ]
        store.put_batch(items)

        assert store.exists("/batch/1.txt")
        assert store.exists("/batch/2.txt")
        assert store.exists("/batch/3.txt")

    def test_delete_batch(self, store: SqlMetadataStore) -> None:
        store.put(_make_metadata("/d1.txt", etag="x"))
        store.put(_make_metadata("/d2.txt", etag="y"))
        store.delete_batch(["/d1.txt", "/d2.txt"])

        assert not store.exists("/d1.txt")
        assert not store.exists("/d2.txt")

    def test_batch_get_content_ids(self, store: SqlMetadataStore) -> None:
        store.put(_make_metadata("/c1.txt", etag="hash_a"))
        store.put(_make_metadata("/c2.txt", etag="hash_b"))

        result = store.batch_get_content_ids(["/c1.txt", "/c2.txt", "/c3.txt"])
        assert result["/c1.txt"] == "hash_a"
        assert result["/c2.txt"] == "hash_b"
        assert result["/c3.txt"] is None


# =========================================================================
# Rename + Implicit Directories
# =========================================================================


class TestRename:
    def test_rename(self, store: SqlMetadataStore) -> None:
        store.put(_make_metadata("/old.txt", etag="r1"))
        store.rename_path("/old.txt", "/new.txt")

        assert store.get("/old.txt") is None
        result = store.get("/new.txt")
        assert result is not None
        assert result.etag == "r1"

    def test_rename_nonexistent_raises(self, store: SqlMetadataStore) -> None:
        with pytest.raises(FileNotFoundError):
            store.rename_path("/nope", "/also_nope")

    def test_rename_logs_operation(
        self, store: SqlMetadataStore, record_store: SQLAlchemyRecordStore
    ) -> None:
        store.put(_make_metadata("/rlog.txt", etag="rr"))
        store.rename_path("/rlog.txt", "/rlog_new.txt")

        with record_store.session_factory() as session:
            ops = session.query(OperationLogModel).filter_by(operation_type="rename").all()
            assert len(ops) == 1
            assert ops[0].path == "/rlog.txt"
            assert ops[0].new_path == "/rlog_new.txt"


class TestImplicitDirectory:
    def test_implicit_dir_exists(self, store: SqlMetadataStore) -> None:
        store.put(_make_metadata("/workspace/file.txt", etag="w1"))
        assert store.is_implicit_directory("/workspace") is True

    def test_implicit_dir_not_exists(self, store: SqlMetadataStore) -> None:
        assert store.is_implicit_directory("/empty") is False


# =========================================================================
# Extended Metadata + Lock Delegation
# =========================================================================


class TestDelegation:
    def test_extended_metadata_without_raft_store(self, store: SqlMetadataStore) -> None:
        """Without raft_store, get returns None, set raises."""
        assert store.get_file_metadata("/f.txt", "key") is None
        with pytest.raises(NotImplementedError):
            store.set_file_metadata("/f.txt", "key", "val")

    def test_lock_without_raft_store(self, store: SqlMetadataStore) -> None:
        """Without raft_store, lock operations raise."""
        with pytest.raises(NotImplementedError):
            store.acquire_lock("/f.txt", "holder1")
        with pytest.raises(NotImplementedError):
            store.release_lock("/f.txt", "holder1")

    def test_close_without_raft_store(self, store: SqlMetadataStore) -> None:
        """Close without raft_store is a no-op."""
        store.close()  # Should not raise
