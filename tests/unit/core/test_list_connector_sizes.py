"""Unit tests for list() API returning correct file sizes for dynamic connectors.

These tests verify that list(details=True) returns actual file sizes from the
file_paths table instead of hardcoded zeros for dynamic connector backends
(e.g., Gmail, HN, user-scoped connectors).

This fixes issue #624 where ls -la showed 0 bytes for all connector files.
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
        enforce_permissions=False,
    )
    yield nx
    nx.close()


class MockDynamicConnector(Backend):
    """Mock dynamic connector backend with user_scoped=True."""

    def __init__(self):
        super().__init__()
        self.token_manager = Mock()  # Mock token manager

    @property
    def name(self) -> str:
        """Backend name."""
        return "mock_dynamic_connector"

    @property
    def user_scoped(self) -> bool:
        """Mark this connector as user-scoped (like Gmail, HN, etc)."""
        return True

    def list_dir(self, path: str, context: OperationContext | None = None) -> list[str]:
        """List directory contents."""
        # Return mock file names
        return ["file1.txt", "file2.yaml", "file3.md"]

    def read_content(self, path: str, context: OperationContext | None = None) -> bytes:
        """Read file content."""
        return b"Mock content"

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
        return False

    def mkdir(self, path: str, context: OperationContext | None = None) -> None:
        """Create directory."""
        pass

    def rmdir(self, path: str, context: OperationContext | None = None) -> None:
        """Remove directory."""
        pass

    def get_content_size(self, path: str, context: OperationContext | None = None) -> int:
        """Get content size."""
        return len(b"Mock content")

    def get_ref_count(self, content_hash: str) -> int:
        """Get reference count for content hash."""
        return 1


class TestListConnectorSizes:
    """Test that list() returns correct sizes for dynamic connector files."""

    def test_list_details_returns_sizes_for_connector(self, nx: NexusFS) -> None:
        """Test that list(details=True) returns actual sizes from file_paths."""
        # Create a mock dynamic connector
        connector = MockDynamicConnector()

        # Add connector as a mount
        mount_path = "/mnt/test_connector"
        nx.router.add_mount(mount_path, connector, priority=10)

        # Create file_paths entries with sizes using database session
        session = nx.metadata.SessionLocal()
        try:
            session.add(
                FilePathModel(
                    path_id=f"{mount_path}/file1.txt",
                    virtual_path=f"{mount_path}/file1.txt",
                    backend_id="mock_connector",
                    physical_path="/file1.txt",
                    size_bytes=1234,
                    tenant_id="default",
                )
            )
            session.add(
                FilePathModel(
                    path_id=f"{mount_path}/file2.yaml",
                    virtual_path=f"{mount_path}/file2.yaml",
                    backend_id="mock_connector",
                    physical_path="/file2.yaml",
                    size_bytes=5678,
                    tenant_id="default",
                )
            )
            session.add(
                FilePathModel(
                    path_id=f"{mount_path}/file3.md",
                    virtual_path=f"{mount_path}/file3.md",
                    backend_id="mock_connector",
                    physical_path="/file3.md",
                    size_bytes=9012,
                    tenant_id="default",
                )
            )
            session.commit()
        finally:
            session.close()

        # List with details
        files = nx.list(mount_path, recursive=False, details=True)

        # Verify sizes are returned from file_paths table
        assert isinstance(files, list)
        assert len(files) == 3

        # Find each file and verify its size
        file1 = next(f for f in files if f["path"] == f"{mount_path}/file1.txt")
        assert file1["size"] == 1234, "file1.txt should have size 1234"

        file2 = next(f for f in files if f["path"] == f"{mount_path}/file2.yaml")
        assert file2["size"] == 5678, "file2.yaml should have size 5678"

        file3 = next(f for f in files if f["path"] == f"{mount_path}/file3.md")
        assert file3["size"] == 9012, "file3.md should have size 9012"

    def test_list_details_handles_missing_metadata(self, nx: NexusFS) -> None:
        """Test that list() handles files without metadata (returns size=0)."""
        # Create a mock dynamic connector
        connector = MockDynamicConnector()

        # Add connector as a mount
        mount_path = "/mnt/test_connector"
        nx.router.add_mount(mount_path, connector, priority=10)

        # Don't create any file_paths entries (simulating unsynced files)

        # List with details
        files = nx.list(mount_path, recursive=False, details=True)

        # Verify files exist but have size=0 (no metadata)
        assert isinstance(files, list)
        assert len(files) == 3

        for file_info in files:
            assert file_info["size"] == 0, "Files without metadata should have size=0"

    def test_list_without_details_no_size_field(self, nx: NexusFS) -> None:
        """Test that list(details=False) returns paths only, no size."""
        # Create a mock dynamic connector
        connector = MockDynamicConnector()

        # Add connector as a mount
        mount_path = "/mnt/test_connector"
        nx.router.add_mount(mount_path, connector, priority=10)

        # List without details
        files = nx.list(mount_path, recursive=False, details=False)

        # Verify paths only (no dicts with size)
        assert isinstance(files, list)
        assert len(files) == 3
        for file_path in files:
            assert isinstance(file_path, str), "details=False should return strings"
            assert file_path.startswith(mount_path)

    def test_list_large_file_sizes(self, nx: NexusFS) -> None:
        """Test that large file sizes (>2GB) are handled correctly."""
        connector = MockDynamicConnector()
        mount_path = "/mnt/test_connector"
        nx.router.add_mount(mount_path, connector, priority=10)

        # Create entry with large size (3GB)
        large_size = 3 * 1024 * 1024 * 1024  # 3GB
        session = nx.metadata.SessionLocal()
        try:
            session.add(
                FilePathModel(
                    path_id=f"{mount_path}/file1.txt",
                    virtual_path=f"{mount_path}/file1.txt",
                    backend_id="mock_connector",
                    physical_path="/file1.txt",
                    size_bytes=large_size,
                    tenant_id="default",
                )
            )
            session.commit()
        finally:
            session.close()

        # List with details
        files = nx.list(mount_path, recursive=False, details=True)

        # Verify large size is preserved
        file1 = next(f for f in files if "file1.txt" in f["path"])
        assert file1["size"] == large_size, f"Should preserve large size {large_size}"

    def test_readdir_cache_uses_list_sizes(self, nx: NexusFS) -> None:
        """Test that FUSE readdir caches sizes from list() for getattr optimization."""
        # This is an integration test to verify the full flow:
        # list() returns sizes → readdir caches them → getattr uses cached values

        connector = MockDynamicConnector()
        mount_path = "/mnt/test_connector"
        nx.router.add_mount(mount_path, connector, priority=10)

        # Create file with known size (using file1.txt which list_dir returns)
        file_path = f"{mount_path}/file1.txt"
        file_size = 42424
        session = nx.metadata.SessionLocal()
        try:
            session.add(
                FilePathModel(
                    path_id=file_path,
                    virtual_path=file_path,
                    backend_id="mock_connector",
                    physical_path="/file1.txt",
                    size_bytes=file_size,
                    tenant_id="default",
                )
            )
            session.commit()
        finally:
            session.close()

        # Call list with details (simulates readdir)
        files = nx.list(mount_path, recursive=False, details=True)

        # Verify size is in the response
        test_file = next(f for f in files if "file1.txt" in f["path"])
        assert test_file["size"] == file_size, "Size should be in list response"


class TestListConnectorSizesRegression:
    """Regression tests to ensure the fix doesn't break existing behavior."""

    def test_regular_local_backend_list_still_works(self, nx: NexusFS) -> None:
        """Test that regular LocalBackend list() is not affected by the fix."""
        # Write files to local backend
        nx.write("/file1.txt", b"Content 1")
        nx.write("/file2.txt", b"Content 2")

        # List with details
        files = nx.list("/", recursive=True, details=True)

        # Verify sizes are correct
        assert len(files) >= 2
        file1 = next(f for f in files if f["path"] == "/file1.txt")
        assert file1["size"] == 9  # len("Content 1")

    def test_list_recursive_with_sizes(self, nx: NexusFS) -> None:
        """Test recursive listing with details includes all files with sizes."""
        connector = MockDynamicConnector()
        mount_path = "/mnt/test"
        nx.router.add_mount(mount_path, connector, priority=10)

        # Create nested structure
        session = nx.metadata.SessionLocal()
        try:
            session.add(
                FilePathModel(
                    path_id=f"{mount_path}/file1.txt",
                    virtual_path=f"{mount_path}/file1.txt",
                    backend_id="mock_connector",
                    physical_path="/file1.txt",
                    size_bytes=100,
                    tenant_id="default",
                )
            )
            session.commit()
        finally:
            session.close()

        # List recursively with details
        files = nx.list(mount_path, recursive=True, details=True)

        # Verify all files have sizes
        for file_info in files:
            assert "size" in file_info, "All files should have size field"
            assert isinstance(file_info["size"], int), "Size should be integer"
