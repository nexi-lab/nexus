"""Tests for time-travel debugging functionality.

Tests the TimeTravelService (services/versioning/time_travel_service.py)
which merges the former storage/time_travel.TimeTravelReader.
"""

import tempfile
from pathlib import Path

import pytest

from nexus.backends.storage.cas_local import CASLocalBackend
from nexus.bricks.versioning.time_travel_service import TimeTravelService
from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.core.config import PermissionConfig
from nexus.factory import create_nexus_fs
from nexus.storage.dict_metastore import DictMetastore
from nexus.storage.operation_logger import OperationLogger
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore


async def _flush(nx) -> None:
    """Flush async write observer so operation_log is up to date."""
    nx.flush_write_observer()


class TestTimeTravelDebug:
    """Test time-travel debugging for reading files at historical operation points."""

    @pytest.fixture
    def temp_dir(self):
        """Create temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    @pytest.fixture
    def record_store(self, temp_dir):
        """Create SQLAlchemyRecordStore for testing."""
        data_dir = Path(temp_dir) / "nexus-data"
        data_dir.mkdir(parents=True, exist_ok=True)
        rs = SQLAlchemyRecordStore(db_path=str(data_dir / "nexus.db"))
        yield rs
        rs.close()

    @pytest.fixture
    def backend(self, temp_dir):
        """Create CASLocalBackend for testing."""
        data_dir = Path(temp_dir) / "nexus-data"
        data_dir.mkdir(parents=True, exist_ok=True)
        return CASLocalBackend(root_path=data_dir)

    @pytest.fixture
    async def nx(self, temp_dir, record_store, backend):
        """Create NexusFS instance for testing.

        Uses RaftMetadataStore. TODO: Time travel depends on FilePathModel
        populated by SQLAlchemy, may need adjustment.
        """
        data_dir = Path(temp_dir) / "nexus-data"
        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            metadata_store = RaftMetadataStore.embedded(str(data_dir / "raft-metadata"))
        except RuntimeError:
            metadata_store = DictMetastore(data_dir / "raft-metadata.json")
        nx = await create_nexus_fs(
            backend=backend,
            metadata_store=metadata_store,
            record_store=record_store,
            permissions=PermissionConfig(enforce=False),
        )
        yield nx
        nx.close()
        metadata_store.close()

    @pytest.fixture
    def time_travel(self, record_store, backend):
        """Create TimeTravelService for testing."""
        return TimeTravelService(
            session_factory=record_store.session_factory,
            backend=backend,
        )

    @pytest.mark.asyncio
    async def test_time_travel_read_file_history(self, nx, record_store, time_travel):
        """Test reading file at different historical points."""
        path = "/workspace/test.txt"

        # Write three versions
        nx.write(path, b"Version 1")
        nx.write(path, b"Version 2")
        nx.write(path, b"Version 3")
        await _flush(nx)

        # Get all operations (most recent first)
        with record_store.session_factory() as session:
            logger = OperationLogger(session)
            ops = logger.list_operations(path=path, limit=10)
            assert len(ops) == 3

            # Operations are in reverse chronological order
            op_v3 = ops[0].operation_id  # Most recent
            op_v2 = ops[1].operation_id
            op_v1 = ops[2].operation_id  # Oldest

        # Read file at each version via service (session-managed)
        state_v1 = time_travel.get_file_at_operation(path, op_v1)
        assert state_v1["content"] == b"Version 1"
        assert state_v1["operation_id"] == op_v1

        state_v2 = time_travel.get_file_at_operation(path, op_v2)
        assert state_v2["content"] == b"Version 2"
        assert state_v2["operation_id"] == op_v2

        state_v3 = time_travel.get_file_at_operation(path, op_v3)
        assert state_v3["content"] == b"Version 3"
        assert state_v3["operation_id"] == op_v3

    @pytest.mark.asyncio
    async def test_time_travel_file_deleted(self, nx, backend, record_store, time_travel):
        """Test reading file that was deleted."""
        path = "/workspace/deleted.txt"

        # Write file
        nx.write(path, b"Content before delete")
        await _flush(nx)

        with record_store.session_factory() as session:
            logger = OperationLogger(session)

            # Get write operation
            ops_after_write = logger.list_operations(path=path, limit=10)
            assert len(ops_after_write) == 1
            op_write = ops_after_write[0].operation_id

        # Delete file — hold extra CAS reference so blob survives unlink
        backend.write_content(b"Content before delete")
        nx.sys_unlink(path)
        await _flush(nx)

        with record_store.session_factory() as session:
            logger = OperationLogger(session)

            # Get delete operation
            ops_after_delete = logger.list_operations(path=path, limit=10)
            assert len(ops_after_delete) == 2
            op_delete = ops_after_delete[0].operation_id

        # Can read file at write operation
        state_before = time_travel.get_file_at_operation(path, op_write)
        assert state_before["content"] == b"Content before delete"

        # Cannot read file at delete operation (it's been deleted)
        with pytest.raises(NexusFileNotFoundError):
            time_travel.get_file_at_operation(path, op_delete)

    @pytest.mark.asyncio
    async def test_time_travel_list_directory(self, nx, record_store, time_travel):
        """Test listing directory at historical operation point."""
        # Create multiple files
        nx.write("/workspace/file1.txt", b"File 1")
        await _flush(nx)

        with record_store.session_factory() as session:
            logger = OperationLogger(session)

            # Get operation after first file
            ops_1 = logger.list_operations(limit=10)
            op_1 = ops_1[0].operation_id

        # Add more files
        nx.write("/workspace/file2.txt", b"File 2")
        await _flush(nx)

        with record_store.session_factory() as session:
            logger = OperationLogger(session)
            ops_2 = logger.list_operations(limit=10)
            op_2 = ops_2[0].operation_id

        nx.write("/workspace/file3.txt", b"File 3")
        await _flush(nx)

        with record_store.session_factory() as session:
            logger = OperationLogger(session)
            ops_3 = logger.list_operations(limit=10)
            op_3 = ops_3[0].operation_id

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

    @pytest.mark.asyncio
    async def test_time_travel_diff_operations(self, nx, record_store, time_travel):
        """Test diffing file state between two operations."""
        path = "/workspace/evolving.txt"

        # Write version 1
        nx.write(path, b"Hello World")
        await _flush(nx)

        with record_store.session_factory() as session:
            logger = OperationLogger(session)
            ops_v1 = logger.list_operations(path=path, limit=10)
            op_v1 = ops_v1[0].operation_id

        # Write version 2 (changed content)
        nx.write(path, b"Hello World - Updated!")
        await _flush(nx)

        with record_store.session_factory() as session:
            logger = OperationLogger(session)
            ops_v2 = logger.list_operations(path=path, limit=10)
            op_v2 = ops_v2[0].operation_id

        # Diff between v1 and v2
        diff = time_travel.diff_operations(path, op_v1, op_v2)

        assert diff["content_changed"] is True
        assert diff["operation_1"] is not None
        assert diff["operation_2"] is not None
        assert diff["operation_1"]["content"] == b"Hello World"
        assert diff["operation_2"]["content"] == b"Hello World - Updated!"
        assert diff["size_diff"] == len(b"Hello World - Updated!") - len(b"Hello World")

    @pytest.mark.asyncio
    async def test_time_travel_diff_file_created(self, nx, record_store, time_travel):
        """Test diff when file was created between operations."""
        # Create a baseline operation
        nx.write("/workspace/baseline.txt", b"Baseline")
        await _flush(nx)

        with record_store.session_factory() as session:
            logger = OperationLogger(session)
            ops_baseline = logger.list_operations(limit=10)
            op_baseline = ops_baseline[0].operation_id

        # Now create the target file
        path = "/workspace/new_file.txt"
        nx.write(path, b"New content")
        await _flush(nx)

        with record_store.session_factory() as session:
            logger = OperationLogger(session)
            ops_after = logger.list_operations(path=path, limit=10)
            op_created = ops_after[0].operation_id

        # Diff between baseline and creation
        diff = time_travel.diff_operations(path, op_baseline, op_created)

        assert diff["content_changed"] is True
        assert diff["operation_1"] is None  # File didn't exist
        assert diff["operation_2"] is not None  # File exists now
        assert diff["operation_2"]["content"] == b"New content"
        assert diff["size_diff"] == len(b"New content")

    @pytest.mark.asyncio
    async def test_time_travel_diff_file_deleted(self, nx, backend, record_store, time_travel):
        """Test diff when file was deleted between operations."""
        path = "/workspace/to_delete.txt"

        # Create file
        nx.write(path, b"Will be deleted")
        await _flush(nx)

        with record_store.session_factory() as session:
            logger = OperationLogger(session)
            ops_created = logger.list_operations(path=path, limit=10)
            op_created = ops_created[0].operation_id

        # Delete file -- hold extra CAS reference so blob survives unlink
        backend.write_content(b"Will be deleted")
        nx.sys_unlink(path)
        await _flush(nx)

        with record_store.session_factory() as session:
            logger = OperationLogger(session)
            ops_deleted = logger.list_operations(path=path, limit=10)
            op_deleted = ops_deleted[0].operation_id

        # Diff between creation and deletion
        diff = time_travel.diff_operations(path, op_created, op_deleted)

        assert diff["content_changed"] is True
        assert diff["operation_1"] is not None  # File existed
        assert diff["operation_2"] is None  # File deleted
        assert diff["operation_1"]["content"] == b"Will be deleted"
        assert diff["size_diff"] == -len(b"Will be deleted")

    @pytest.mark.asyncio
    async def test_time_travel_with_agent_id(self, nx, record_store, time_travel):
        """Test time-travel with agent-specific operations using context parameter."""
        from nexus.contracts.types import OperationContext

        # Use context parameter with agent ID
        context = OperationContext(user_id="test", groups=[], agent_id="agent-1", zone_id="root")

        path = "/workspace/agent_file.txt"
        nx.write(path, b"Agent 1 content", context=context)
        await _flush(nx)

        with record_store.session_factory() as session:
            logger = OperationLogger(session)

            # Verify operation has agent_id
            ops = logger.list_operations(path=path, agent_id="agent-1", limit=10)
            assert len(ops) == 1
            assert ops[0].agent_id == "agent-1"
            op_id = ops[0].operation_id

        # Read file at operation (no zone_id filter — this test validates
        # agent_id tracking, not zone isolation. The OperationLogModel and
        # FilePathModel may record different zone_id values when context
        # has zone_id=None, so omitting zone_id avoids a false mismatch.)
        state = time_travel.get_file_at_operation(path, op_id)
        assert state["content"] == b"Agent 1 content"

    def test_time_travel_nonexistent_operation(self, time_travel):
        """Test error handling for nonexistent operation ID."""
        with pytest.raises(NexusFileNotFoundError):
            time_travel.get_file_at_operation("/any/path", "fake-operation-id")

    @pytest.mark.asyncio
    async def test_time_travel_metadata_preservation(self, nx, record_store, time_travel):
        """Test that metadata is preserved in historical reads."""
        path = "/workspace/metadata_test.txt"

        # Write file
        nx.write(path, b"Content")
        await _flush(nx)

        # Set permissions using ReBAC (v0.6.0+)
        nx.service("rebac").rebac_create_sync(
            subject=("user", "testowner"), relation="direct_owner", object=("file", path)
        )

        # Write again to create a new version
        nx.write(path, b"Updated content")
        await _flush(nx)

        with record_store.session_factory() as session:
            logger = OperationLogger(session)

            # Get the second write operation
            ops = logger.list_operations(path=path, operation_type="write", limit=10)
            assert len(ops) >= 1
            op_id = ops[0].operation_id

        # Read file at second write
        state = time_travel.get_file_at_operation(path, op_id)

        # Verify metadata is preserved
        assert state["content"] == b"Updated content"
        # Note: Metadata from previous state should be in the snapshot
        assert "metadata" in state
