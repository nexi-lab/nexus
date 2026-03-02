"""Unit tests for operation logging and undo capability."""

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from nexus import LocalBackend, NexusFS
from nexus.core.config import ParseConfig, PermissionConfig
from nexus.factory import create_nexus_fs
from nexus.storage.operation_logger import OperationLogger
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore


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
def local_backend(temp_dir: Path) -> LocalBackend:
    """Create a LocalBackend for direct CAS operations in tests."""
    return LocalBackend(temp_dir)


@pytest.fixture
def nx(
    temp_dir: Path, local_backend: LocalBackend, record_store: SQLAlchemyRecordStore
) -> Generator[NexusFS, None, None]:
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


def test_write_operation_logged(nx: NexusFS, record_store: SQLAlchemyRecordStore) -> None:
    """Test that write operations are logged."""
    path = "/test.txt"
    content = b"Test content"

    # Write file
    nx.sys_write(path, content)

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


def test_write_update_operation_logged(nx: NexusFS, record_store: SQLAlchemyRecordStore) -> None:
    """Test that updating a file logs the previous version."""
    path = "/test.txt"
    content1 = b"Version 1"
    content2 = b"Version 2"

    # Write initial version (use write() to get metadata dict with etag)
    result1 = nx.write(path, content1)
    old_hash = result1["etag"]

    # Update file
    nx.sys_write(path, content2)

    # Check operation log
    with record_store.session_factory() as session:
        logger = OperationLogger(session)
        operations = logger.list_operations(path=path, limit=10)

        # Should have 2 operations: initial write and update
        assert len(operations) == 2

        # Most recent operation should have snapshot of previous version
        latest = operations[0]
        assert latest.operation_type == "write"
        assert latest.path == path
        assert latest.snapshot_hash == old_hash  # Should store previous content hash

        # Check metadata snapshot
        metadata = logger.get_metadata_snapshot(latest)
        assert metadata is not None
        assert metadata["size"] == len(content1)
        assert metadata["version"] == 1


def test_delete_operation_logged(nx: NexusFS, record_store: SQLAlchemyRecordStore) -> None:
    """Test that delete operations are logged with snapshot."""
    path = "/test.txt"
    content = b"Test content"

    # Write and then delete (use write() to get metadata dict with etag)
    result = nx.write(path, content)
    content_hash = result["etag"]
    nx.sys_unlink(path)

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


def test_rename_operation_logged(nx: NexusFS, record_store: SQLAlchemyRecordStore) -> None:
    """Test that rename operations are logged."""
    old_path = "/old.txt"
    new_path = "/new.txt"
    content = b"Test content"

    # Write and then rename
    nx.sys_write(old_path, content)
    nx.sys_rename(old_path, new_path)

    # Check operation log
    with record_store.session_factory() as session:
        logger = OperationLogger(session)
        operations = logger.list_operations(operation_type="rename", limit=10)

        assert len(operations) >= 1
        latest = operations[0]
        assert latest.operation_type == "rename"
        assert latest.path == old_path
        assert latest.new_path == new_path
        assert latest.status == "success"


def test_operation_log_filtering_by_agent(nx: NexusFS, record_store: SQLAlchemyRecordStore) -> None:
    """Test filtering operations by agent ID using context parameter."""
    from nexus.contracts.types import OperationContext

    # Use context parameter with different agent IDs
    context1 = OperationContext(user_id="test", groups=[], agent_id="agent-1")
    nx.sys_write("/file1.txt", b"Content 1", context=context1)

    context2 = OperationContext(user_id="test", groups=[], agent_id="agent-2")
    nx.sys_write("/file2.txt", b"Content 2", context=context2)

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


def test_operation_log_filtering_by_type(nx: NexusFS, record_store: SQLAlchemyRecordStore) -> None:
    """Test filtering operations by type."""
    path = "/test.txt"

    # Perform various operations
    nx.sys_write(path, b"Content")
    nx.sys_write(path, b"Updated")
    nx.sys_rename(path, "/renamed.txt")
    nx.sys_unlink("/renamed.txt")

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


def test_get_path_history(nx: NexusFS, record_store: SQLAlchemyRecordStore) -> None:
    """Test getting operation history for a specific path."""
    path = "/test.txt"

    # Perform multiple operations on same path
    nx.sys_write(path, b"Version 1")
    nx.sys_write(path, b"Version 2")
    nx.sys_write(path, b"Version 3")

    # Check path history
    with record_store.session_factory() as session:
        logger = OperationLogger(session)
        history = logger.get_path_history(path, limit=10)

        assert len(history) == 3
        assert all(op.path == path for op in history)
        assert all(op.operation_type == "write" for op in history)


def test_get_last_operation(nx: NexusFS, record_store: SQLAlchemyRecordStore) -> None:
    """Test getting the last operation."""
    # Perform operations
    nx.sys_write("/file1.txt", b"Content 1")
    nx.sys_write("/file2.txt", b"Content 2")

    # Get last operation
    with record_store.session_factory() as session:
        logger = OperationLogger(session)
        last_op = logger.get_last_operation(status="success")

        assert last_op is not None
        assert last_op.path == "/file2.txt"  # Most recent
        assert last_op.operation_type == "write"


def test_undo_write_new_file(nx: NexusFS) -> None:
    """Test undoing a write operation for a new file (should delete it)."""
    path = "/test.txt"
    content = b"Test content"

    # Write file
    nx.sys_write(path, content)
    assert nx.sys_access(path)

    # Undo by deleting the file
    nx.sys_unlink(path)
    assert not nx.sys_access(path)


def test_undo_write_update(
    nx: NexusFS, local_backend: LocalBackend, record_store: SQLAlchemyRecordStore
) -> None:
    """Test undoing a write operation that updated an existing file."""
    path = "/test.txt"
    content1 = b"Version 1"
    content2 = b"Version 2"

    # Write initial version (use write() to get metadata dict with etag)
    result1 = nx.write(path, content1)
    old_hash = result1["etag"]

    # Update file
    nx.sys_write(path, content2)

    # Get the update operation
    with record_store.session_factory() as session:
        logger = OperationLogger(session)
        operations = logger.list_operations(path=path, limit=1)

        assert len(operations) == 1
        last_op = operations[0]
        assert last_op.snapshot_hash == old_hash

        # Undo by restoring old content
        old_content = local_backend.read_content(last_op.snapshot_hash)
        nx.sys_write(path, old_content)

        # Verify restoration
        restored_content = nx.sys_read(path)
        assert restored_content == content1


def test_undo_delete(
    nx: NexusFS, local_backend: LocalBackend, record_store: SQLAlchemyRecordStore
) -> None:
    """Test undoing a delete operation."""
    path = "/test.txt"
    content = b"Test content"

    # Write and delete (use write() to get metadata dict with etag)
    result = nx.write(path, content)
    content_hash = result["etag"]
    local_backend.write_content(content)  # Hold extra CAS reference so blob survives unlink
    nx.sys_unlink(path)
    assert not nx.sys_access(path)

    # Get delete operation
    with record_store.session_factory() as session:
        logger = OperationLogger(session)
        last_op = logger.get_last_operation(operation_type="delete")

        assert last_op is not None
        assert last_op.snapshot_hash == content_hash

        # Undo by restoring from snapshot
        restored_content = local_backend.read_content(last_op.snapshot_hash)
        nx.sys_write(path, restored_content)

        # Verify restoration
        assert nx.sys_access(path)
        assert nx.sys_read(path) == content


def test_undo_rename(nx: NexusFS, record_store: SQLAlchemyRecordStore) -> None:
    """Test undoing a rename operation."""
    old_path = "/old.txt"
    new_path = "/new.txt"
    content = b"Test content"

    # Write and rename
    nx.sys_write(old_path, content)
    nx.sys_rename(old_path, new_path)
    assert not nx.sys_access(old_path)
    assert nx.sys_access(new_path)

    # Get rename operation
    with record_store.session_factory() as session:
        logger = OperationLogger(session)
        last_op = logger.get_last_operation(operation_type="rename")

        assert last_op is not None
        assert last_op.path == old_path
        assert last_op.new_path == new_path

        # Undo by renaming back
        nx.sys_rename(new_path, old_path)

        # Verify undo
        assert nx.sys_access(old_path)
        assert not nx.sys_access(new_path)
