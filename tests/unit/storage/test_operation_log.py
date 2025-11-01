"""Unit tests for operation logging and undo capability."""

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from nexus import LocalBackend, NexusFS
from nexus.storage.operation_logger import OperationLogger


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def nx(temp_dir: Path) -> Generator[NexusFS, None, None]:
    """Create a NexusFS instance for testing."""
    nx = NexusFS(
        backend=LocalBackend(temp_dir),
        db_path=temp_dir / "metadata.db",
        auto_parse=False,
        enforce_permissions=False,  # Disable permissions for tests
    )
    yield nx
    nx.close()


def test_write_operation_logged(nx: NexusFS) -> None:
    """Test that write operations are logged."""
    path = "/test.txt"
    content = b"Test content"

    # Write file
    nx.write(path, content)

    # Check operation log
    with nx.metadata.SessionLocal() as session:
        logger = OperationLogger(session)
        operations = logger.list_operations(limit=10)

        assert len(operations) >= 1
        latest = operations[0]
        assert latest.operation_type == "write"
        assert latest.path == path
        assert latest.status == "success"
        assert latest.snapshot_hash is None  # New file, no previous version


def test_write_update_operation_logged(nx: NexusFS) -> None:
    """Test that updating a file logs the previous version."""
    path = "/test.txt"
    content1 = b"Version 1"
    content2 = b"Version 2"

    # Write initial version
    result1 = nx.write(path, content1)
    old_hash = result1["etag"]

    # Update file
    nx.write(path, content2)

    # Check operation log
    with nx.metadata.SessionLocal() as session:
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


def test_delete_operation_logged(nx: NexusFS) -> None:
    """Test that delete operations are logged with snapshot."""
    path = "/test.txt"
    content = b"Test content"

    # Write and then delete
    result = nx.write(path, content)
    content_hash = result["etag"]
    nx.delete(path)

    # Check operation log
    with nx.metadata.SessionLocal() as session:
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


def test_rename_operation_logged(nx: NexusFS) -> None:
    """Test that rename operations are logged."""
    old_path = "/old.txt"
    new_path = "/new.txt"
    content = b"Test content"

    # Write and then rename
    nx.write(old_path, content)
    nx.rename(old_path, new_path)

    # Check operation log
    with nx.metadata.SessionLocal() as session:
        logger = OperationLogger(session)
        operations = logger.list_operations(operation_type="rename", limit=10)

        assert len(operations) >= 1
        latest = operations[0]
        assert latest.operation_type == "rename"
        assert latest.path == old_path
        assert latest.new_path == new_path
        assert latest.status == "success"


def test_operation_log_filtering_by_agent(nx: NexusFS) -> None:
    """Test filtering operations by agent ID using context parameter."""
    from nexus.core.permissions_enhanced import EnhancedOperationContext

    # Use context parameter with different agent IDs
    context1 = EnhancedOperationContext(user="test", groups=[], agent_id="agent-1")
    nx.write("/file1.txt", b"Content 1", context=context1)

    context2 = EnhancedOperationContext(user="test", groups=[], agent_id="agent-2")
    nx.write("/file2.txt", b"Content 2", context=context2)

    # Check operation log filtering
    with nx.metadata.SessionLocal() as session:
        logger = OperationLogger(session)

        # Filter by agent-1
        ops_agent1 = logger.list_operations(agent_id="agent-1", limit=10)
        assert len(ops_agent1) >= 1
        assert all(op.agent_id == "agent-1" for op in ops_agent1)

        # Filter by agent-2
        ops_agent2 = logger.list_operations(agent_id="agent-2", limit=10)
        assert len(ops_agent2) >= 1
        assert all(op.agent_id == "agent-2" for op in ops_agent2)


def test_operation_log_filtering_by_type(nx: NexusFS) -> None:
    """Test filtering operations by type."""
    path = "/test.txt"

    # Perform various operations
    nx.write(path, b"Content")
    nx.write(path, b"Updated")
    nx.rename(path, "/renamed.txt")
    nx.delete("/renamed.txt")

    # Check operation log filtering
    with nx.metadata.SessionLocal() as session:
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


def test_get_path_history(nx: NexusFS) -> None:
    """Test getting operation history for a specific path."""
    path = "/test.txt"

    # Perform multiple operations on same path
    nx.write(path, b"Version 1")
    nx.write(path, b"Version 2")
    nx.write(path, b"Version 3")

    # Check path history
    with nx.metadata.SessionLocal() as session:
        logger = OperationLogger(session)
        history = logger.get_path_history(path, limit=10)

        assert len(history) == 3
        assert all(op.path == path for op in history)
        assert all(op.operation_type == "write" for op in history)


def test_get_last_operation(nx: NexusFS) -> None:
    """Test getting the last operation."""
    # Perform operations
    nx.write("/file1.txt", b"Content 1")
    nx.write("/file2.txt", b"Content 2")

    # Get last operation
    with nx.metadata.SessionLocal() as session:
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
    nx.write(path, content)
    assert nx.exists(path)

    # Undo by deleting the file
    nx.delete(path)
    assert not nx.exists(path)


def test_undo_write_update(nx: NexusFS) -> None:
    """Test undoing a write operation that updated an existing file."""
    path = "/test.txt"
    content1 = b"Version 1"
    content2 = b"Version 2"

    # Write initial version
    result1 = nx.write(path, content1)
    old_hash = result1["etag"]

    # Update file
    nx.write(path, content2)

    # Get the update operation
    with nx.metadata.SessionLocal() as session:
        logger = OperationLogger(session)
        operations = logger.list_operations(path=path, limit=1)

        assert len(operations) == 1
        last_op = operations[0]
        assert last_op.snapshot_hash == old_hash

        # Undo by restoring old content
        old_content = nx.backend.read_content(last_op.snapshot_hash)
        nx.write(path, old_content)

        # Verify restoration
        restored_content = nx.read(path)
        assert restored_content == content1


def test_undo_delete(nx: NexusFS) -> None:
    """Test undoing a delete operation."""
    path = "/test.txt"
    content = b"Test content"

    # Write and delete
    result = nx.write(path, content)
    content_hash = result["etag"]
    nx.delete(path)
    assert not nx.exists(path)

    # Get delete operation
    with nx.metadata.SessionLocal() as session:
        logger = OperationLogger(session)
        last_op = logger.get_last_operation(operation_type="delete")

        assert last_op is not None
        assert last_op.snapshot_hash == content_hash

        # Undo by restoring from snapshot
        restored_content = nx.backend.read_content(last_op.snapshot_hash)
        nx.write(path, restored_content)

        # Verify restoration
        assert nx.exists(path)
        assert nx.read(path) == content


def test_undo_rename(nx: NexusFS) -> None:
    """Test undoing a rename operation."""
    old_path = "/old.txt"
    new_path = "/new.txt"
    content = b"Test content"

    # Write and rename
    nx.write(old_path, content)
    nx.rename(old_path, new_path)
    assert not nx.exists(old_path)
    assert nx.exists(new_path)

    # Get rename operation
    with nx.metadata.SessionLocal() as session:
        logger = OperationLogger(session)
        last_op = logger.get_last_operation(operation_type="rename")

        assert last_op is not None
        assert last_op.path == old_path
        assert last_op.new_path == new_path

        # Undo by renaming back
        nx.rename(new_path, old_path)

        # Verify undo
        assert nx.exists(old_path)
        assert not nx.exists(new_path)
