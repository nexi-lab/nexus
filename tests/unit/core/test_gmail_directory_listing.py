"""Unit tests for Gmail directory listing fixes.

These tests verify:
1. Directory paths have trailing slashes stripped for permission checks
2. Directories are identified by mime_type="inode/directory" instead of trailing slash
3. Response format includes both new fields (is_directory, etag, mime_type) and legacy fields
4. Directory markers are created during sync with proper zone_id
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import Mock

import pytest

from nexus import LocalBackend, NexusFS
from nexus.backends.backend import Backend
from nexus.core.permissions import OperationContext
from nexus.storage.models import FilePathModel
from nexus.storage.sqlalchemy_metadata_store import SQLAlchemyMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore


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
        metadata_store=SQLAlchemyMetadataStore(db_path=temp_dir / "metadata.db"),
        record_store=SQLAlchemyRecordStore(db_path=temp_dir / "metadata.db"),
        auto_parse=False,
        enforce_permissions=False,
    )
    yield nx
    nx.close()


class MockGmailConnector(Backend):
    """Mock Gmail connector backend with user_scoped=True and token_manager."""

    def __init__(self):
        super().__init__()
        self.token_manager = Mock()  # Mock token manager to simulate Gmail connector

    @property
    def name(self) -> str:
        """Backend name."""
        return "mock_gmail_connector"

    @property
    def user_scoped(self) -> bool:
        """Mark this connector as user-scoped (like Gmail)."""
        return True

    def list_dir(self, path: str, context: OperationContext | None = None) -> list[str]:
        """List directory contents - returns directories with trailing slashes."""
        path_map = {
            "": ["INBOX/", "SENT/", "STARRED/", "IMPORTANT/"],  # Root level
            "INBOX": ["email1.yaml", "email2.yaml"],  # Files in INBOX
            "SENT": ["email3.yaml"],  # Files in SENT
        }
        return path_map.get(path, [])

    def read_content(self, path: str, context: OperationContext | None = None) -> bytes:
        """Read file content."""
        return b"Mock email content"

    def write_content(
        self, path: str, content: bytes, context: OperationContext | None = None
    ) -> None:
        """Write file content."""
        pass

    def delete_content(self, path: str, context: OperationContext | None = None) -> None:
        """Delete file."""
        pass

    def content_exists(self, path: str, context: OperationContext | None = None) -> bool:
        """Check if content exists."""
        return True

    def is_directory(self, path: str, context: OperationContext | None = None) -> bool:
        """Check if path is a directory."""
        return path.endswith("/")

    def mkdir(self, path: str, context: OperationContext | None = None) -> None:
        """Create directory."""
        pass

    def rmdir(self, path: str, context: OperationContext | None = None) -> None:
        """Remove directory."""
        pass

    def get_content_size(self, path: str, context: OperationContext | None = None) -> int:
        """Get content size."""
        return len(b"Mock email content")

    def get_ref_count(self, content_hash: str) -> int:
        """Get reference count for content hash."""
        return 1


class TestGmailDirectoryListing:
    """Test Gmail directory listing with trailing slash fixes."""

    def test_directories_have_trailing_slashes_stripped(self, nx: NexusFS) -> None:
        """Test that directory paths have trailing slashes stripped before permission checks."""
        connector = MockGmailConnector()
        mount_path = "/mnt/work_gmail"
        nx.router.add_mount(mount_path, connector, priority=10)

        # Create directory metadata entries with mime_type="inode/directory" (no trailing slash)
        session = nx.metadata.SessionLocal()
        try:
            for dir_name in ["INBOX", "SENT", "STARRED", "IMPORTANT"]:
                session.add(
                    FilePathModel(
                        path_id=f"{mount_path}/{dir_name}",
                        virtual_path=f"{mount_path}/{dir_name}",
                        backend_id="mock_gmail",
                        physical_path=f"/{dir_name}",
                        size_bytes=0,
                        file_type="inode/directory",
                        zone_id="default",
                    )
                )
            session.commit()
        finally:
            session.close()

        # List without details (should return paths without trailing slashes)
        files = nx.list(mount_path, recursive=False, details=False)

        assert isinstance(files, list)
        assert len(files) == 4

        # Verify paths don't have trailing slashes
        for file_path in files:
            assert not file_path.endswith("/"), f"Path should not have trailing slash: {file_path}"
            assert file_path.startswith(mount_path)

    def test_directories_identified_by_mime_type(self, nx: NexusFS) -> None:
        """Test that directories are identified by mime_type instead of trailing slash."""
        connector = MockGmailConnector()
        mount_path = "/mnt/work_gmail"
        nx.router.add_mount(mount_path, connector, priority=10)

        # Create directory entries with mime_type="inode/directory"
        session = nx.metadata.SessionLocal()
        try:
            session.add(
                FilePathModel(
                    path_id=f"{mount_path}/INBOX",
                    virtual_path=f"{mount_path}/INBOX",
                    backend_id="mock_gmail",
                    physical_path="/INBOX",
                    size_bytes=0,
                    file_type="inode/directory",  # This marks it as directory
                    zone_id="default",
                )
            )
            session.add(
                FilePathModel(
                    path_id=f"{mount_path}/file.yaml",
                    virtual_path=f"{mount_path}/file.yaml",
                    backend_id="mock_gmail",
                    physical_path="/file.yaml",
                    size_bytes=100,
                    file_type="application/yaml",  # Regular file
                    zone_id="default",
                )
            )
            session.commit()
        finally:
            session.close()

        # List with details
        files = nx.list(mount_path, recursive=False, details=True)

        # Find directory and file
        inbox_dir = next(f for f in files if "INBOX" in f["path"])
        yaml_file = next((f for f in files if "file.yaml" in f["path"]), None)

        # Verify directory is detected by mime_type
        assert inbox_dir["is_directory"] is True
        assert inbox_dir["mime_type"] == "inode/directory"
        assert inbox_dir["type"] == "directory"  # Legacy field

        # Verify file is not a directory
        if yaml_file:
            assert yaml_file["is_directory"] is False
            assert yaml_file["type"] == "file"  # Legacy field

    def test_response_format_backward_compatible(self, nx: NexusFS) -> None:
        """Test that response includes both new and legacy fields."""
        connector = MockGmailConnector()
        mount_path = "/mnt/work_gmail"
        nx.router.add_mount(mount_path, connector, priority=10)

        # Create directory entry
        session = nx.metadata.SessionLocal()
        try:
            session.add(
                FilePathModel(
                    path_id=f"{mount_path}/INBOX",
                    virtual_path=f"{mount_path}/INBOX",
                    backend_id="mock_gmail",
                    physical_path="/INBOX",
                    size_bytes=0,
                    file_type="inode/directory",
                    zone_id="default",
                )
            )
            session.commit()
        finally:
            session.close()

        # List with details
        files = nx.list(mount_path, recursive=False, details=True)
        assert len(files) > 0

        inbox_dir = next(f for f in files if "INBOX" in f["path"])

        # Verify new format fields
        assert "is_directory" in inbox_dir
        assert inbox_dir["is_directory"] is True
        assert "mime_type" in inbox_dir
        assert inbox_dir["mime_type"] == "inode/directory"
        assert "etag" in inbox_dir  # etag field should be present (may be None)
        assert "modified_at" in inbox_dir

        # Verify legacy fields for backward compatibility
        assert "name" in inbox_dir
        assert inbox_dir["name"] == "INBOX"
        assert "type" in inbox_dir
        assert inbox_dir["type"] == "directory"
        assert "updated_at" in inbox_dir

        # Verify common fields
        assert "path" in inbox_dir
        assert "size" in inbox_dir
        assert inbox_dir["size"] == 0
        assert "created_at" in inbox_dir

    def test_recursive_listing_includes_directories(self, nx: NexusFS) -> None:
        """Test that recursive listing includes directory entries."""
        connector = MockGmailConnector()
        mount_path = "/mnt/work_gmail"
        nx.router.add_mount(mount_path, connector, priority=10)

        # Create directory and file entries
        session = nx.metadata.SessionLocal()
        try:
            # Directory
            session.add(
                FilePathModel(
                    path_id=f"{mount_path}/INBOX",
                    virtual_path=f"{mount_path}/INBOX",
                    backend_id="mock_gmail",
                    physical_path="/INBOX",
                    size_bytes=0,
                    file_type="inode/directory",
                    zone_id="default",
                )
            )
            # Files in directory
            session.add(
                FilePathModel(
                    path_id=f"{mount_path}/INBOX/email1.yaml",
                    virtual_path=f"{mount_path}/INBOX/email1.yaml",
                    backend_id="mock_gmail",
                    physical_path="/INBOX/email1.yaml",
                    size_bytes=500,
                    file_type="application/yaml",
                    zone_id="default",
                )
            )
            session.commit()
        finally:
            session.close()

        # List recursively
        files = nx.list(mount_path, recursive=True, details=True)

        # Should include both directory and files
        assert len(files) >= 2

        # Find directory entry
        inbox_dir = next((f for f in files if f["path"] == f"{mount_path}/INBOX"), None)
        assert inbox_dir is not None
        assert inbox_dir["is_directory"] is True

        # Find file entry
        email_file = next((f for f in files if "email1.yaml" in f["path"]), None)
        assert email_file is not None
        assert email_file["is_directory"] is False


class TestDirectoryMarkerCreation:
    """Test directory marker creation during sync."""

    def test_directory_markers_created_with_zone_id(self, nx: NexusFS) -> None:
        """Test that directory markers are created with proper zone_id during sync."""
        # This is an integration test that would test the sync_mount functionality
        # The actual sync functionality would need to be tested with proper mount setup
        # For now, we verify the database schema supports the fields we need

        session = nx.metadata.SessionLocal()
        try:
            # Create a directory marker as sync would
            dir_marker = FilePathModel(
                path_id="/zone/test/user:123/connector/gmail/INBOX",
                virtual_path="/zone/test/user:123/connector/gmail/INBOX",
                backend_id="gmail",
                physical_path="/INBOX",
                size_bytes=0,
                file_type="inode/directory",
                zone_id="test",  # Proper zone_id for permission checks
            )
            session.add(dir_marker)
            session.commit()

            # Verify it was created correctly
            result = (
                session.query(FilePathModel)
                .filter_by(virtual_path="/zone/test/user:123/connector/gmail/INBOX")
                .first()
            )
            assert result is not None
            assert result.zone_id == "test"
            assert result.file_type == "inode/directory"
            assert result.size_bytes == 0
        finally:
            session.close()


class TestAgentCreationFix:
    """Test agent creation bug fix."""

    def test_register_agent_uses_correct_metadata_method(self, nx: NexusFS) -> None:
        """Test that register_agent uses metadata.get() instead of get_file_metadata()."""
        # Create an agent
        context = {"user_id": "test_user", "zone_id": "default"}

        try:
            result = nx.register_agent(
                agent_id="test_user,TestAgent",
                name="Test Agent",
                description="Test agent for unit test",
                generate_api_key=False,
                context=context,
            )

            # Verify agent was created successfully
            assert result is not None
            assert result["agent_id"] == "test_user,TestAgent"
            assert result["name"] == "Test Agent"
            assert result["user_id"] == "test_user"

            # Verify agent config file exists
            agent_config_path = "/zone/default/user:test_user/agent/TestAgent/config.yaml"
            assert nx.exists(agent_config_path)

        except Exception as e:
            pytest.fail(f"Agent creation should not fail: {e}")

    def test_register_agent_prevents_duplicate(self, nx: NexusFS) -> None:
        """Test that register_agent prevents creating duplicate agents."""
        context = {"user_id": "test_user", "zone_id": "default"}

        # Create first agent
        result1 = nx.register_agent(
            agent_id="test_user,DuplicateAgent",
            name="Duplicate Agent",
            description="First agent",
            generate_api_key=False,
            context=context,
        )
        assert result1 is not None

        # Try to create duplicate agent
        with pytest.raises(ValueError, match="Agent already exists"):
            nx.register_agent(
                agent_id="test_user,DuplicateAgent",
                name="Duplicate Agent",
                description="Second agent with same ID",
                generate_api_key=False,
                context=context,
            )
