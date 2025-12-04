"""Tests for time-travel debugging functionality."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import nexus
from nexus.core.exceptions import NotFoundError


class TestTimeTravelDebug:
    """Test time-travel debugging for reading files at historical operation points."""

    @pytest.fixture
    def temp_dir(self):
        """Create temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    @pytest.fixture
    def nx(self, temp_dir):
        """Create NexusFS instance for testing."""
        data_dir = Path(temp_dir) / "nexus-data"
        data_dir.mkdir(parents=True, exist_ok=True)

        nx = nexus.connect(
            config={
                "data_dir": str(data_dir),
                "enforce_permissions": False,  # Disable permissions for tests
                "backend": "local",
            }
        )
        yield nx
        nx.close()

    def test_time_travel_read_file_history(self, nx):
        """Test reading file at different historical points."""
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.time_travel import TimeTravelReader

        path = "/workspace/test.txt"

        # Write three versions
        nx.write(path, b"Version 1")
        nx.write(path, b"Version 2")
        nx.write(path, b"Version 3")

        # Get all operations (most recent first)
        with nx.metadata.SessionLocal() as session:
            logger = OperationLogger(session)
            ops = logger.list_operations(path=path, limit=10)
            assert len(ops) == 3

            # Operations are in reverse chronological order
            op_v3 = ops[0].operation_id  # Most recent
            op_v2 = ops[1].operation_id
            op_v1 = ops[2].operation_id  # Oldest

            # Create time-travel reader
            time_travel = TimeTravelReader(session, nx.backend)

            # Read file at version 1
            state_v1 = time_travel.get_file_at_operation(path, op_v1)
            assert state_v1["content"] == b"Version 1"
            assert state_v1["operation_id"] == op_v1

            # Read file at version 2
            state_v2 = time_travel.get_file_at_operation(path, op_v2)
            assert state_v2["content"] == b"Version 2"
            assert state_v2["operation_id"] == op_v2

            # Read file at version 3
            state_v3 = time_travel.get_file_at_operation(path, op_v3)
            assert state_v3["content"] == b"Version 3"
            assert state_v3["operation_id"] == op_v3

    def test_time_travel_file_deleted(self, nx):
        """Test reading file that was deleted."""
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.time_travel import TimeTravelReader

        path = "/workspace/deleted.txt"

        # Write file
        nx.write(path, b"Content before delete")

        with nx.metadata.SessionLocal() as session:
            logger = OperationLogger(session)

            # Get write operation
            ops_after_write = logger.list_operations(path=path, limit=10)
            assert len(ops_after_write) == 1
            op_write = ops_after_write[0].operation_id

            # Delete file
            nx.delete(path)

            # Get delete operation
            ops_after_delete = logger.list_operations(path=path, limit=10)
            assert len(ops_after_delete) == 2
            op_delete = ops_after_delete[0].operation_id

            # Create time-travel reader
            time_travel = TimeTravelReader(session, nx.backend)

            # Can read file at write operation
            state_before = time_travel.get_file_at_operation(path, op_write)
            assert state_before["content"] == b"Content before delete"

            # Cannot read file at delete operation (it's been deleted)
            with pytest.raises(NotFoundError):
                time_travel.get_file_at_operation(path, op_delete)

    def test_time_travel_list_directory(self, nx):
        """Test listing directory at historical operation point."""
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.time_travel import TimeTravelReader

        # Create multiple files
        nx.write("/workspace/file1.txt", b"File 1")

        with nx.metadata.SessionLocal() as session:
            logger = OperationLogger(session)

            # Get operation after first file
            ops_1 = logger.list_operations(limit=10)
            op_1 = ops_1[0].operation_id

            # Add more files
            nx.write("/workspace/file2.txt", b"File 2")
            ops_2 = logger.list_operations(limit=10)
            op_2 = ops_2[0].operation_id

            nx.write("/workspace/file3.txt", b"File 3")
            ops_3 = logger.list_operations(limit=10)
            op_3 = ops_3[0].operation_id

            # Create time-travel reader
            time_travel = TimeTravelReader(session, nx.backend)

            # List directory at op_1 (only file1 exists)
            files_at_op1 = time_travel.list_files_at_operation("/workspace", op_1)
            assert len(files_at_op1) == 1
            assert files_at_op1[0]["path"] == "/workspace/file1.txt"

            # List directory at op_2 (file1 and file2 exist)
            files_at_op2 = time_travel.list_files_at_operation("/workspace", op_2)
            assert len(files_at_op2) == 2
            paths = [f["path"] for f in files_at_op2]
            assert "/workspace/file1.txt" in paths
            assert "/workspace/file2.txt" in paths

            # List directory at op_3 (all three files exist)
            files_at_op3 = time_travel.list_files_at_operation("/workspace", op_3)
            assert len(files_at_op3) == 3
            paths = [f["path"] for f in files_at_op3]
            assert "/workspace/file1.txt" in paths
            assert "/workspace/file2.txt" in paths
            assert "/workspace/file3.txt" in paths

    def test_time_travel_diff_operations(self, nx):
        """Test diffing file state between two operations."""
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.time_travel import TimeTravelReader

        path = "/workspace/evolving.txt"

        # Write version 1
        nx.write(path, b"Hello World")

        with nx.metadata.SessionLocal() as session:
            logger = OperationLogger(session)
            ops_v1 = logger.list_operations(path=path, limit=10)
            op_v1 = ops_v1[0].operation_id

            # Write version 2 (changed content)
            nx.write(path, b"Hello World - Updated!")

            ops_v2 = logger.list_operations(path=path, limit=10)
            op_v2 = ops_v2[0].operation_id

            # Create time-travel reader
            time_travel = TimeTravelReader(session, nx.backend)

            # Diff between v1 and v2
            diff = time_travel.diff_operations(path, op_v1, op_v2)

            assert diff["content_changed"] is True
            assert diff["operation_1"] is not None
            assert diff["operation_2"] is not None
            assert diff["operation_1"]["content"] == b"Hello World"
            assert diff["operation_2"]["content"] == b"Hello World - Updated!"
            assert diff["size_diff"] == len(b"Hello World - Updated!") - len(b"Hello World")

    def test_time_travel_diff_file_created(self, nx):
        """Test diff when file was created between operations."""
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.time_travel import TimeTravelReader

        # Create a baseline operation (write different file)
        nx.write("/workspace/baseline.txt", b"Baseline")

        with nx.metadata.SessionLocal() as session:
            logger = OperationLogger(session)
            ops_baseline = logger.list_operations(limit=10)
            op_baseline = ops_baseline[0].operation_id

            # Now create the target file
            path = "/workspace/new_file.txt"
            nx.write(path, b"New content")

            ops_after = logger.list_operations(path=path, limit=10)
            op_created = ops_after[0].operation_id

            # Create time-travel reader
            time_travel = TimeTravelReader(session, nx.backend)

            # Diff between baseline and creation
            diff = time_travel.diff_operations(path, op_baseline, op_created)

            assert diff["content_changed"] is True
            assert diff["operation_1"] is None  # File didn't exist
            assert diff["operation_2"] is not None  # File exists now
            assert diff["operation_2"]["content"] == b"New content"
            assert diff["size_diff"] == len(b"New content")

    def test_time_travel_diff_file_deleted(self, nx):
        """Test diff when file was deleted between operations."""
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.time_travel import TimeTravelReader

        path = "/workspace/to_delete.txt"

        # Create file
        nx.write(path, b"Will be deleted")

        with nx.metadata.SessionLocal() as session:
            logger = OperationLogger(session)
            ops_created = logger.list_operations(path=path, limit=10)
            op_created = ops_created[0].operation_id

            # Delete file
            nx.delete(path)

            ops_deleted = logger.list_operations(path=path, limit=10)
            op_deleted = ops_deleted[0].operation_id

            # Create time-travel reader
            time_travel = TimeTravelReader(session, nx.backend)

            # Diff between creation and deletion
            diff = time_travel.diff_operations(path, op_created, op_deleted)

            assert diff["content_changed"] is True
            assert diff["operation_1"] is not None  # File existed
            assert diff["operation_2"] is None  # File deleted
            assert diff["operation_1"]["content"] == b"Will be deleted"
            assert diff["size_diff"] == -len(b"Will be deleted")

    def test_time_travel_with_agent_id(self, nx):
        """Test time-travel with agent-specific operations using context parameter."""
        from nexus.core.permissions_enhanced import EnhancedOperationContext
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.time_travel import TimeTravelReader

        # Use context parameter with agent ID
        context = EnhancedOperationContext(user="test", groups=[], agent_id="agent-1")

        path = "/workspace/agent_file.txt"
        nx.write(path, b"Agent 1 content", context=context)

        with nx.metadata.SessionLocal() as session:
            logger = OperationLogger(session)

            # Verify operation has agent_id
            ops = logger.list_operations(path=path, agent_id="agent-1", limit=10)
            assert len(ops) == 1
            assert ops[0].agent_id == "agent-1"
            op_id = ops[0].operation_id
            op_tenant_id = ops[0].tenant_id

            # Create time-travel reader
            time_travel = TimeTravelReader(session, nx.backend)

            # Read file at operation (use the operation's tenant_id)
            state = time_travel.get_file_at_operation(path, op_id, tenant_id=op_tenant_id)
            assert state["content"] == b"Agent 1 content"

    def test_time_travel_nonexistent_operation(self, nx):
        """Test error handling for nonexistent operation ID."""
        from nexus.storage.time_travel import TimeTravelReader

        with nx.metadata.SessionLocal() as session:
            time_travel = TimeTravelReader(session, nx.backend)

            # Try to read with fake operation ID
            with pytest.raises(NotFoundError):
                time_travel.get_operation_by_id("fake-operation-id")

    def test_time_travel_metadata_preservation(self, nx):
        """Test that metadata is preserved in historical reads."""
        from nexus.storage.operation_logger import OperationLogger
        from nexus.storage.time_travel import TimeTravelReader

        path = "/workspace/metadata_test.txt"

        # Write file
        nx.write(path, b"Content")

        # Set permissions using ReBAC (v0.6.0+)
        nx.rebac_create(
            subject=("user", "testowner"), relation="direct_owner", object=("file", path)
        )

        # Write again to create a new version
        nx.write(path, b"Updated content")

        with nx.metadata.SessionLocal() as session:
            logger = OperationLogger(session)

            # Get the second write operation
            ops = logger.list_operations(path=path, operation_type="write", limit=10)
            assert len(ops) >= 1
            op_id = ops[0].operation_id

            # Create time-travel reader
            time_travel = TimeTravelReader(session, nx.backend)

            # Read file at second write
            state = time_travel.get_file_at_operation(path, op_id)

            # Verify metadata is preserved
            assert state["content"] == b"Updated content"
            # Note: Metadata from previous state should be in the snapshot
            assert "metadata" in state
