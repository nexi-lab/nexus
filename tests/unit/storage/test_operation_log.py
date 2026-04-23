"""Unit tests for operation logging and undo capability."""

import tempfile
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest

from nexus import CASLocalBackend, NexusFS
from nexus.core.config import ParseConfig, PermissionConfig
from nexus.factory import create_nexus_fs
from nexus.storage.operation_logger import OperationLogger
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore


async def _flush(nx) -> None:
    """Flush async write observer so operation_log rows are visible."""
    nx.flush_write_observer()


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def record_store(temp_dir: Path) -> Generator[SQLAlchemyRecordStore, None, None]:
    """Create a SQLAlchemyRecordStore for testing."""
    rs = SQLAlchemyRecordStore(db_path=temp_dir / "metadata.db")
    yield rs
    rs.close()


@pytest.fixture
def local_backend(temp_dir: Path) -> CASLocalBackend:
    """Create a CASLocalBackend for direct CAS operations in tests."""
    return CASLocalBackend(temp_dir)


@pytest.fixture
def nx(
    temp_dir: Path, local_backend: CASLocalBackend, record_store: SQLAlchemyRecordStore
) -> AsyncGenerator[NexusFS, None]:
    """Create a NexusFS instance for testing."""
    nx = create_nexus_fs(
        backend=local_backend,
        metadata_store=RaftMetadataStore.embedded(str(temp_dir / "raft-metadata")),
        record_store=record_store,
        parsing=ParseConfig(auto_parse=False),
        permissions=PermissionConfig(enforce=False),
    )
    yield nx
    nx.close()


@pytest.mark.asyncio
def test_write_operation_logged(nx: NexusFS, record_store: SQLAlchemyRecordStore) -> None:
    """Test that write operations are logged."""
    path = "/test.txt"
    content = b"Test content"

    # Write file
    nx.write(path, content)
    await _flush(nx)

    # Check operation log
    with record_store.session_factory() as session:
        logger = OperationLogger(session)
        operations = logger.list_operations(limit=10)

        assert len(operations) >= 1
        latest = operations[0]
        assert latest.operation_type == "write"
        assert latest.path == path
        assert latest.status == "success"
        assert latest.snapshot_hash is None  # New file, no previous version


@pytest.mark.asyncio
def test_write_update_operation_logged(nx: NexusFS, record_store: SQLAlchemyRecordStore) -> None:
    """Test that updating a file logs the previous version."""
    path = "/test.txt"
    content1 = b"Version 1"
    content2 = b"Version 2"

    # Write initial version (use write() to get metadata dict with etag)
    result1 = nx.write(path, content1)
    old_hash = result1["etag"]

    # Update file
    nx.write(path, content2)
    await _flush(nx)

    # Check operation log
    with record_store.session_factory() as session:
        logger = OperationLogger(session)
        operations = logger.list_operations(path=path, limit=10)

        # Should have 2 operations: initial write and update
        assert len(operations) == 2

        # Most recent operation should have snapshot of previous version hash
        latest = operations[0]
        assert latest.operation_type == "write"
        assert latest.path == path
        assert latest.snapshot_hash == old_hash  # Should store previous content hash

        # metadata_snapshot stores the NEW metadata (for MCL replay, Issue #2929)
        metadata = logger.get_metadata_snapshot(latest)
        assert metadata is not None
        assert metadata["size"] == len(content2)
        assert metadata["version"] == 2


@pytest.mark.asyncio
def test_delete_operation_logged(nx: NexusFS, record_store: SQLAlchemyRecordStore) -> None:
    """Test that delete operations are logged with snapshot."""
    path = "/test.txt"
    content = b"Test content"

    # Write and then delete (use write() to get metadata dict with etag)
    result = nx.write(path, content)
    content_hash = result["etag"]
    nx.sys_unlink(path)
    await _flush(nx)

    # Check operation log
    with record_store.session_factory() as session:
        logger = OperationLogger(session)
        operations = logger.list_operations(operation_type="delete", limit=10)

        assert len(operations) >= 1
        latest = operations[0]
        assert latest.operation_type == "delete"
        assert latest.path == path
        assert latest.status == "success"
        assert latest.snapshot_hash == content_hash  # Should store content for undo

        # Check metadata snapshot
        metadata = logger.get_metadata_snapshot(latest)
        assert metadata is not None
        assert metadata["size"] == len(content)


@pytest.mark.asyncio
def test_rename_operation_logged(nx: NexusFS, record_store: SQLAlchemyRecordStore) -> None:
    """Test that rename operations are logged.

    The PR #2929 two-row rename pattern creates 2 operation_log rows:
    Row 1 (DELETE old URN): path=old_path, new_path=new_path
    Row 2 (UPSERT new URN): path=new_path
    """
    old_path = "/old.txt"
    new_path = "/new.txt"
    content = b"Test content"

    # Write and then rename
    nx.write(old_path, content)
    nx.sys_rename(old_path, new_path)
    await _flush(nx)

    # Check operation log — two-row rename pattern (Issue #2929)
    with record_store.session_factory() as session:
        logger = OperationLogger(session)
        operations = logger.list_operations(operation_type="rename", limit=10)

        # Two rows: DELETE old URN + UPSERT new URN
        assert len(operations) >= 2
        # Most recent row (UPSERT new URN)
        upsert_row = operations[0]
        assert upsert_row.operation_type == "rename"
        assert upsert_row.path == new_path
        assert upsert_row.status == "success"
        # Older row (DELETE old URN)
        delete_row = operations[1]
        assert delete_row.operation_type == "rename"
        assert delete_row.path == old_path
        assert delete_row.new_path == new_path
        assert delete_row.status == "success"


@pytest.mark.asyncio
def test_operation_log_filtering_by_agent(nx: NexusFS, record_store: SQLAlchemyRecordStore) -> None:
    """Test filtering operations by agent ID using context parameter."""
    from nexus.contracts.types import OperationContext

    # Use context parameter with different agent IDs
    context1 = OperationContext(user_id="test", groups=[], agent_id="agent-1")
    nx.write("/file1.txt", b"Content 1", context=context1)

    context2 = OperationContext(user_id="test", groups=[], agent_id="agent-2")
    nx.write("/file2.txt", b"Content 2", context=context2)
    await _flush(nx)

    # Check operation log filtering
    with record_store.session_factory() as session:
        logger = OperationLogger(session)

        # Filter by agent-1
        ops_agent1 = logger.list_operations(agent_id="agent-1", limit=10)
        assert len(ops_agent1) >= 1
        assert all(op.agent_id == "agent-1" for op in ops_agent1)

        # Filter by agent-2
        ops_agent2 = logger.list_operations(agent_id="agent-2", limit=10)
        assert len(ops_agent2) >= 1
        assert all(op.agent_id == "agent-2" for op in ops_agent2)


@pytest.mark.asyncio
def test_operation_log_filtering_by_type(nx: NexusFS, record_store: SQLAlchemyRecordStore) -> None:
    """Test filtering operations by type."""
    path = "/test.txt"

    # Perform various operations
    nx.write(path, b"Content")
    nx.write(path, b"Updated")
    nx.sys_rename(path, "/renamed.txt")
    nx.sys_unlink("/renamed.txt")
    await _flush(nx)

    # Check operation log filtering
    with record_store.session_factory() as session:
        logger = OperationLogger(session)

        # Filter by write
        write_ops = logger.list_operations(operation_type="write", limit=10)
        assert len(write_ops) >= 2
        assert all(op.operation_type == "write" for op in write_ops)

        # Filter by delete
        delete_ops = logger.list_operations(operation_type="delete", limit=10)
        assert len(delete_ops) >= 1
        assert all(op.operation_type == "delete" for op in delete_ops)

        # Filter by rename
        rename_ops = logger.list_operations(operation_type="rename", limit=10)
        assert len(rename_ops) >= 1
        assert all(op.operation_type == "rename" for op in rename_ops)


@pytest.mark.asyncio
def test_get_path_history(nx: NexusFS, record_store: SQLAlchemyRecordStore) -> None:
    """Test getting operation history for a specific path."""
    path = "/test.txt"

    # Perform multiple operations on same path
    nx.write(path, b"Version 1")
    nx.write(path, b"Version 2")
    nx.write(path, b"Version 3")
    await _flush(nx)

    # Check path history
    with record_store.session_factory() as session:
        logger = OperationLogger(session)
        history = logger.get_path_history(path, limit=10)

        assert len(history) == 3
        assert all(op.path == path for op in history)
        assert all(op.operation_type == "write" for op in history)


@pytest.mark.asyncio
def test_get_last_operation(nx: NexusFS, record_store: SQLAlchemyRecordStore) -> None:
    """Test getting the last operation."""
    # Perform operations
    nx.write("/file1.txt", b"Content 1")
    nx.write("/file2.txt", b"Content 2")
    await _flush(nx)

    # Get last operation
    with record_store.session_factory() as session:
        logger = OperationLogger(session)
        last_op = logger.get_last_operation(status="success")

        assert last_op is not None
        assert last_op.path == "/file2.txt"  # Most recent
        assert last_op.operation_type == "write"


@pytest.mark.asyncio
def test_undo_write_new_file(nx: NexusFS) -> None:
    """Test undoing a write operation for a new file (should delete it)."""
    path = "/test.txt"
    content = b"Test content"

    # Write file
    nx.write(path, content)
    assert nx.access(path)

    # Undo by deleting the file
    nx.sys_unlink(path)
    assert not nx.access(path)


@pytest.mark.asyncio
def test_undo_write_update(
    nx: NexusFS, local_backend: CASLocalBackend, record_store: SQLAlchemyRecordStore
) -> None:
    """Test undoing a write operation that updated an existing file."""
    path = "/test.txt"
    content1 = b"Version 1"
    content2 = b"Version 2"

    # Write initial version (use write() to get metadata dict with etag)
    result1 = nx.write(path, content1)
    old_hash = result1["etag"]

    # Update file
    nx.write(path, content2)
    await _flush(nx)

    # Get the update operation
    with record_store.session_factory() as session:
        logger = OperationLogger(session)
        operations = logger.list_operations(path=path, limit=1)

        assert len(operations) == 1
        last_op = operations[0]
        assert last_op.snapshot_hash == old_hash

        # Undo by restoring old content
        old_content = local_backend.read_content(last_op.snapshot_hash)
        nx.write(path, old_content)

        # Verify restoration
        restored_content = nx.sys_read(path)
        assert restored_content == content1


@pytest.mark.asyncio
def test_undo_delete(
    nx: NexusFS, local_backend: CASLocalBackend, record_store: SQLAlchemyRecordStore
) -> None:
    """Test undoing a delete operation."""
    path = "/test.txt"
    content = b"Test content"

    # Write and delete (use write() to get metadata dict with etag)
    result = nx.write(path, content)
    content_hash = result["etag"]
    local_backend.write_content(content)  # Hold extra CAS reference so blob survives unlink
    nx.sys_unlink(path)
    await _flush(nx)
    assert not nx.access(path)

    # Get delete operation
    with record_store.session_factory() as session:
        logger = OperationLogger(session)
        last_op = logger.get_last_operation(operation_type="delete")

        assert last_op is not None
        assert last_op.snapshot_hash == content_hash

        # Undo by restoring from snapshot
        restored_content = local_backend.read_content(last_op.snapshot_hash)
        nx.write(path, restored_content)

        # Verify restoration
        assert nx.access(path)
        assert nx.sys_read(path) == content


@pytest.mark.asyncio
def test_undo_rename(nx: NexusFS, record_store: SQLAlchemyRecordStore) -> None:
    """Test undoing a rename operation.

    With two-row rename (Issue #2929), get_last_operation returns the
    most recent row (UPSERT new URN, path=new_path). The older row
    (DELETE old URN) has path=old_path and new_path=new_path.
    """
    old_path = "/old.txt"
    new_path = "/new.txt"
    content = b"Test content"

    # Write and rename
    nx.write(old_path, content)
    nx.sys_rename(old_path, new_path)
    await _flush(nx)
    assert not nx.access(old_path)
    assert nx.access(new_path)

    # Get rename operations — two-row pattern
    with record_store.session_factory() as session:
        logger = OperationLogger(session)
        rename_ops = logger.list_operations(operation_type="rename", limit=10)

        # Two rows from the two-row rename pattern
        assert len(rename_ops) >= 2
        # Older row (DELETE old URN) has both paths
        delete_row = rename_ops[1]
        assert delete_row.path == old_path
        assert delete_row.new_path == new_path

        # Undo by renaming back
        nx.sys_rename(new_path, old_path)

        # Verify undo
        assert nx.access(old_path)
        assert not nx.access(new_path)
