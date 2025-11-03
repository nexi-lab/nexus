"""Unit tests for NexusFS filesystem with GCS backend."""

import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import Mock

import pytest

from nexus import NexusFS
from nexus.core.exceptions import InvalidPathError, NexusFileNotFoundError


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_gcs_backend() -> Generator[Mock, None, None]:
    """Create a mock GCS backend."""
    mock_backend = Mock()
    mock_backend.name = "gcs"
    mock_backend.bucket_name = "test-bucket"
    mock_backend.project_id = "test-project"
    yield mock_backend


@pytest.fixture
def remote_fs(temp_dir: Path, mock_gcs_backend: Mock) -> Generator[NexusFS, None, None]:
    """Create a NexusFS instance with mocked GCS backend."""
    db_path = temp_dir / "test-metadata.db"
    fs = NexusFS(
        backend=mock_gcs_backend,
        db_path=db_path,
        auto_parse=False,
        enforce_permissions=False,  # Disable auto-parsing for unit tests
    )
    yield fs
    fs.close()


class TestNexusFSInitialization:
    """Test NexusFS initialization."""

    def test_init_creates_metadata_db(self, temp_dir: Path, mock_gcs_backend: Mock) -> None:
        """Test that initialization creates metadata database."""
        db_path = temp_dir / "metadata.db"
        assert not db_path.exists()

        fs = NexusFS(backend=mock_gcs_backend, db_path=db_path)

        assert db_path.exists()
        fs.close()

    def test_init_with_default_db_path(self, mock_gcs_backend: Mock) -> None:
        """Test initialization with default database path."""
        fs = NexusFS(backend=mock_gcs_backend)

        assert fs.backend.bucket_name == "test-bucket"
        # Should use default path
        assert fs.metadata is not None
        fs.close()

        # Clean up default db
        Path("./nexus-remote-metadata.db").unlink(missing_ok=True)

    def test_init_with_credentials(self, temp_dir: Path, mock_gcs_backend: Mock) -> None:
        """Test initialization with explicit credentials."""
        db_path = temp_dir / "metadata.db"
        # Configure mock backend with credentials
        mock_gcs_backend.bucket_name = "test-bucket"
        mock_gcs_backend.project_id = "my-project"
        mock_gcs_backend.credentials_path = "/path/to/creds.json"

        fs = NexusFS(
            backend=mock_gcs_backend,
            db_path=db_path,
        )

        assert fs.backend.bucket_name == "test-bucket"
        assert fs.backend.project_id == "my-project"
        fs.close()

    def test_init_with_tenant_and_agent(self, temp_dir: Path, mock_gcs_backend: Mock) -> None:
        """Test initialization with tenant and agent context."""
        db_path = temp_dir / "metadata.db"
        fs = NexusFS(
            backend=mock_gcs_backend,
            db_path=db_path,
            tenant_id="tenant-123",
            agent_id="agent-456",
            is_admin=True,
        )

        assert fs.tenant_id == "tenant-123"
        assert fs.agent_id == "agent-456"
        assert fs.is_admin is True
        fs.close()


class TestPathValidation:
    """Test path validation."""

    def test_validate_path_empty(self, remote_fs: NexusFS) -> None:
        """Test that empty paths are rejected."""
        with pytest.raises(InvalidPathError) as exc_info:
            remote_fs._validate_path("")

        assert "cannot be empty" in str(exc_info.value).lower()

    def test_validate_path_adds_leading_slash(self, remote_fs: NexusFS) -> None:
        """Test that paths without leading slash get one added."""
        result = remote_fs._validate_path("test/path")
        assert result == "/test/path"

    def test_validate_path_invalid_characters(self, remote_fs: NexusFS) -> None:
        """Test that paths with invalid characters are rejected."""
        with pytest.raises(InvalidPathError):
            remote_fs._validate_path("/path/with\0null")

        with pytest.raises(InvalidPathError):
            remote_fs._validate_path("/path/with\nnewline")

    def test_validate_path_parent_traversal(self, remote_fs: NexusFS) -> None:
        """Test that parent directory traversal is rejected."""
        with pytest.raises(InvalidPathError) as exc_info:
            remote_fs._validate_path("/path/../etc/passwd")

        assert ".." in str(exc_info.value)


class TestWriteAndRead:
    """Test write and read operations."""

    def test_write_and_read_basic(self, remote_fs: NexusFS) -> None:
        """Test basic write and read operations."""
        content = b"Hello, NexusFS!"
        path = "/test/file.txt"

        # Mock backend write_content to return a hash
        content_hash = "abc123def456"
        remote_fs.backend.write_content.return_value = content_hash

        # Mock router.route to return the backend
        from unittest.mock import Mock

        mock_route = Mock()
        mock_route.backend = remote_fs.backend
        mock_route.readonly = False
        remote_fs.router.route = Mock(return_value=mock_route)

        # Write file
        remote_fs.write(path, content)

        # Verify backend was called with context parameter
        assert remote_fs.backend.write_content.call_count == 1
        call_args = remote_fs.backend.write_content.call_args
        assert call_args[0][0] == content  # First positional arg

        # Mock backend read_content
        remote_fs.backend.read_content.return_value = content

        # Read file
        result = remote_fs.read(path)
        assert result == content

        # Verify backend was called with correct hash and context
        assert remote_fs.backend.read_content.call_count == 1
        call_args = remote_fs.backend.read_content.call_args
        assert call_args[0][0] == content_hash  # First positional arg

    def test_write_creates_metadata(self, remote_fs: NexusFS) -> None:
        """Test that writing creates metadata."""
        content = b"Test content"
        path = "/test.txt"
        content_hash = "hash123"

        remote_fs.backend.write_content.return_value = content_hash
        remote_fs.write(path, content)

        # Check metadata exists
        assert remote_fs.exists(path)

        # Check metadata content
        meta = remote_fs.metadata.get(path)
        assert meta is not None
        assert meta.path == path
        assert meta.size == len(content)
        assert meta.backend_name == "gcs"
        assert meta.etag == content_hash
        assert meta.physical_path == content_hash

    def test_write_overwrites_existing(self, remote_fs: NexusFS) -> None:
        """Test that writing overwrites existing file.

        With version tracking (v0.3.5), old content is preserved
        for accessing previous versions, not deleted.
        """
        path = "/test.txt"
        content1 = b"Version 1"
        content2 = b"Version 2"
        hash1 = "hash1"
        hash2 = "hash2"

        # Write first version
        remote_fs.backend.write_content.return_value = hash1
        remote_fs.write(path, content1)

        meta1 = remote_fs.metadata.get(path)
        assert meta1 is not None
        assert meta1.etag == hash1
        assert meta1.version == 1

        # Write second version
        remote_fs.backend.write_content.return_value = hash2
        remote_fs.write(path, content2)

        # With version tracking (v0.3.5), old content is NOT deleted
        # because it's needed for accessing previous versions
        remote_fs.backend.delete_content.assert_not_called()

        meta2 = remote_fs.metadata.get(path)
        assert meta2 is not None
        assert meta2.etag == hash2
        assert meta2.size == len(content2)
        assert meta2.version == 2  # Version incremented

    def test_read_nonexistent_file(self, remote_fs: NexusFS) -> None:
        """Test reading a nonexistent file."""
        with pytest.raises(NexusFileNotFoundError):
            remote_fs.read("/nonexistent.txt")

    def test_read_file_without_etag(self, remote_fs: NexusFS) -> None:
        """Test reading a file with missing etag."""
        path = "/test.txt"

        # Create metadata entry without etag
        from datetime import UTC, datetime

        from nexus.core.metadata import FileMetadata

        meta = FileMetadata(
            path=path,
            backend_name="gcs",
            physical_path="placeholder",  # Need physical_path for validation
            size=0,
            etag=None,  # Missing etag
            created_at=datetime.now(UTC),
            modified_at=datetime.now(UTC),
            version=1,
        )
        remote_fs.metadata.put(meta)

        with pytest.raises(NexusFileNotFoundError):
            remote_fs.read(path)


class TestAppend:
    """Test append operations."""

    def test_append_to_new_file(self, remote_fs: NexusFS) -> None:
        """Test appending to a new file (creates it)."""
        content = b"First line\n"
        path = "/test/log.txt"
        content_hash = "hash123"

        # Mock backend write_content
        remote_fs.backend.write_content.return_value = content_hash

        # Mock router.route
        from unittest.mock import Mock

        mock_route = Mock()
        mock_route.backend = remote_fs.backend
        mock_route.readonly = False
        remote_fs.router.route = Mock(return_value=mock_route)

        # Append to new file
        result = remote_fs.append(path, content)

        # Verify it was written
        assert result["etag"] == content_hash
        assert result["size"] == len(content)
        assert result["version"] == 1

        # Verify backend was called
        assert remote_fs.backend.write_content.call_count == 1
        call_args = remote_fs.backend.write_content.call_args
        assert call_args[0][0] == content

    def test_append_to_existing_file(self, remote_fs: NexusFS) -> None:
        """Test appending to an existing file."""
        path = "/test/log.txt"
        initial_content = b"Line 1\n"
        append_content = b"Line 2\n"
        combined_content = initial_content + append_content
        hash1 = "hash1"
        hash2 = "hash2"

        # Mock router.route
        from unittest.mock import Mock

        mock_route = Mock()
        mock_route.backend = remote_fs.backend
        mock_route.readonly = False
        remote_fs.router.route = Mock(return_value=mock_route)

        # Create initial file
        remote_fs.backend.write_content.return_value = hash1
        remote_fs.write(path, initial_content)

        # Mock read_content to return initial content
        remote_fs.backend.read_content.return_value = initial_content

        # Append new content
        remote_fs.backend.write_content.return_value = hash2
        result = remote_fs.append(path, append_content)

        # Verify the combined content was written
        assert result["etag"] == hash2
        assert result["size"] == len(combined_content)
        assert result["version"] == 2  # Version incremented

        # Verify backend write was called with combined content
        call_args = remote_fs.backend.write_content.call_args
        assert call_args[0][0] == combined_content

    def test_append_multiple_times(self, remote_fs: NexusFS) -> None:
        """Test appending multiple times (like building a log)."""
        path = "/test/log.txt"

        # Mock router.route
        from unittest.mock import Mock

        mock_route = Mock()
        mock_route.backend = remote_fs.backend
        mock_route.readonly = False
        remote_fs.router.route = Mock(return_value=mock_route)

        # Append three times
        lines = [b"Line 1\n", b"Line 2\n", b"Line 3\n"]
        accumulated = b""

        for i, line in enumerate(lines, 1):
            # Mock read to return accumulated content
            remote_fs.backend.read_content.return_value = accumulated

            # Append
            hash_val = f"hash{i}"
            remote_fs.backend.write_content.return_value = hash_val
            result = remote_fs.append(path, line)

            # Update accumulated content
            accumulated += line

            # Verify
            assert result["version"] == i
            assert result["size"] == len(accumulated)

    def test_append_with_string_content(self, remote_fs: NexusFS) -> None:
        """Test appending with string content (auto-converts to bytes)."""
        path = "/test/log.txt"
        content = "Text line\n"
        content_bytes = content.encode("utf-8")
        content_hash = "hash123"

        # Mock router.route
        from unittest.mock import Mock

        mock_route = Mock()
        mock_route.backend = remote_fs.backend
        mock_route.readonly = False
        remote_fs.router.route = Mock(return_value=mock_route)

        # Append with string
        remote_fs.backend.write_content.return_value = content_hash
        _result = remote_fs.append(path, content)

        # Verify bytes were written
        call_args = remote_fs.backend.write_content.call_args
        assert call_args[0][0] == content_bytes

    def test_append_jsonl_use_case(self, remote_fs: NexusFS) -> None:
        """Test typical JSONL use case - appending JSON lines."""
        import json

        path = "/logs/events.jsonl"

        # Mock router.route
        from unittest.mock import Mock

        mock_route = Mock()
        mock_route.backend = remote_fs.backend
        mock_route.readonly = False
        remote_fs.router.route = Mock(return_value=mock_route)

        # Append multiple JSON records
        events = [
            {"timestamp": "2024-01-01T00:00:00Z", "event": "login", "user": "alice"},
            {"timestamp": "2024-01-01T00:01:00Z", "event": "upload", "user": "bob"},
            {"timestamp": "2024-01-01T00:02:00Z", "event": "logout", "user": "alice"},
        ]

        accumulated = b""
        for i, event in enumerate(events, 1):
            # Convert to JSONL line
            line = (json.dumps(event) + "\n").encode("utf-8")

            # Mock read
            remote_fs.backend.read_content.return_value = accumulated

            # Append
            hash_val = f"hash{i}"
            remote_fs.backend.write_content.return_value = hash_val
            remote_fs.append(path, line)

            accumulated += line

        # Verify final size
        meta = remote_fs.metadata.get(path)
        assert meta is not None
        assert meta.size == len(accumulated)

    def test_append_with_optimistic_concurrency_control(self, remote_fs: NexusFS) -> None:
        """Test append with if_match for concurrency control."""
        from nexus.core.exceptions import ConflictError

        path = "/test/log.txt"
        initial_content = b"Line 1\n"
        hash1 = "hash1"

        # Mock router.route
        from unittest.mock import Mock

        mock_route = Mock()
        mock_route.backend = remote_fs.backend
        mock_route.readonly = False
        remote_fs.router.route = Mock(return_value=mock_route)

        # Create initial file
        remote_fs.backend.write_content.return_value = hash1
        remote_fs.write(path, initial_content)

        # Mock read to return initial content
        remote_fs.backend.read_content.return_value = initial_content

        # Try to append with wrong etag
        with pytest.raises(ConflictError) as exc_info:
            remote_fs.append(path, b"Line 2\n", if_match="wrong_hash")

        assert (
            "conflict" in str(exc_info.value).lower() or "expected" in str(exc_info.value).lower()
        )

    def test_append_to_readonly_path(self, remote_fs: NexusFS) -> None:
        """Test that appending to readonly path raises PermissionError."""
        # Mock readonly route
        from unittest.mock import Mock

        mock_route = Mock()
        mock_route.readonly = True
        remote_fs.router.route = Mock(return_value=mock_route)

        with pytest.raises(PermissionError) as exc_info:
            remote_fs.append("/readonly/file.txt", b"content")

        assert "read-only" in str(exc_info.value).lower()


class TestDelete:
    """Test delete operations."""

    def test_delete_existing_file(self, remote_fs: NexusFS) -> None:
        """Test deleting an existing file."""
        path = "/test.txt"
        content = b"test"
        content_hash = "hash123"

        # Mock router.route to return the backend
        from unittest.mock import Mock

        mock_route = Mock()
        mock_route.backend = remote_fs.backend
        mock_route.readonly = False
        remote_fs.router.route = Mock(return_value=mock_route)

        # Create file
        remote_fs.backend.write_content.return_value = content_hash
        remote_fs.write(path, content)

        assert remote_fs.exists(path)

        # Delete file
        remote_fs.delete(path)

        # Verify backend delete was called with context parameter
        assert remote_fs.backend.delete_content.call_count == 1
        call_args = remote_fs.backend.delete_content.call_args
        assert call_args[0][0] == content_hash  # First positional arg

        # Verify metadata is gone
        assert not remote_fs.exists(path)

    def test_delete_nonexistent_file(self, remote_fs: NexusFS) -> None:
        """Test deleting a nonexistent file."""
        with pytest.raises(NexusFileNotFoundError):
            remote_fs.delete("/nonexistent.txt")


class TestExists:
    """Test file existence checking."""

    def test_exists_true(self, remote_fs: NexusFS) -> None:
        """Test exists returns True for existing file."""
        path = "/test.txt"
        content_hash = "hash123"

        remote_fs.backend.write_content.return_value = content_hash
        remote_fs.write(path, b"test")

        assert remote_fs.exists(path) is True

    def test_exists_false(self, remote_fs: NexusFS) -> None:
        """Test exists returns False for nonexistent file."""
        assert remote_fs.exists("/nonexistent.txt") is False

    def test_exists_invalid_path(self, remote_fs: NexusFS) -> None:
        """Test exists returns False for invalid path."""
        assert remote_fs.exists("/invalid/../path") is False


class TestList:
    """Test list operations."""

    def test_list_empty_directory(self, remote_fs: NexusFS) -> None:
        """Test listing an empty directory."""
        files = remote_fs.list("/workspace")
        assert files == []

    def test_list_with_files(self, remote_fs: NexusFS) -> None:
        """Test listing directory with files."""
        # Create some files
        files_to_create = [
            "/workspace/file1.txt",
            "/workspace/file2.txt",
            "/workspace/subdir/file3.txt",
        ]

        for path in files_to_create:
            remote_fs.backend.write_content.return_value = f"hash_{path}"
            remote_fs.write(path, b"content")

        # List recursively
        files = remote_fs.list("/workspace", recursive=True)
        assert len(files) == 3
        assert all(f in files for f in files_to_create)

    def test_list_non_recursive(self, remote_fs: NexusFS) -> None:
        """Test listing directory non-recursively."""
        # Create files at different levels
        remote_fs.backend.write_content.return_value = "hash1"
        remote_fs.write("/workspace/file1.txt", b"content")

        remote_fs.backend.write_content.return_value = "hash2"
        remote_fs.write("/workspace/file2.txt", b"content")

        remote_fs.backend.write_content.return_value = "hash3"
        remote_fs.write("/workspace/subdir/file3.txt", b"content")

        # List non-recursively (includes directories now)
        files = remote_fs.list("/workspace", recursive=False)

        assert len(files) == 3  # file1.txt + file2.txt + subdir
        assert "/workspace/file1.txt" in files
        assert "/workspace/file2.txt" in files
        assert "/workspace/subdir" in files  # Directory is now included
        assert "/workspace/subdir/file3.txt" not in files  # But nested file is not

    def test_list_with_details(self, remote_fs: NexusFS) -> None:
        """Test listing with detailed metadata."""
        path = "/test.txt"
        content = b"test content"
        content_hash = "hash123"

        remote_fs.backend.write_content.return_value = content_hash
        remote_fs.write(path, content)

        # List with details
        files = remote_fs.list("/", recursive=True, details=True)

        assert len(files) == 1
        file_info = files[0]
        assert file_info["path"] == path
        assert file_info["size"] == len(content)
        assert file_info["etag"] == content_hash
        assert "modified_at" in file_info
        assert "created_at" in file_info


class TestGlob:
    """Test glob pattern matching."""

    def test_glob_wildcard(self, remote_fs: NexusFS) -> None:
        """Test glob with wildcard pattern."""
        # Create test files
        files = [
            "/data/file1.txt",
            "/data/file2.txt",
            "/data/file3.csv",
            "/other/file4.txt",
        ]

        for path in files:
            remote_fs.backend.write_content.return_value = f"hash_{path}"
            remote_fs.write(path, b"content")

        # Find all .txt files in /data
        matches = remote_fs.glob("*.txt", "/data")

        assert len(matches) == 2
        assert "/data/file1.txt" in matches
        assert "/data/file2.txt" in matches
        assert "/data/file3.csv" not in matches

    def test_glob_recursive(self, remote_fs: NexusFS) -> None:
        """Test glob with recursive pattern."""
        # Create test files
        files = [
            "/src/main.py",
            "/src/lib/utils.py",
            "/tests/test_main.py",
        ]

        for path in files:
            remote_fs.backend.write_content.return_value = f"hash_{path}"
            remote_fs.write(path, b"content")

        # Find all Python files recursively
        matches = remote_fs.glob("**/*.py")

        assert len(matches) == 3
        assert all(f in matches for f in files)

    def test_glob_no_matches(self, remote_fs: NexusFS) -> None:
        """Test glob with no matches."""
        remote_fs.backend.write_content.return_value = "hash"
        remote_fs.write("/test.txt", b"content")

        matches = remote_fs.glob("*.pdf")
        assert matches == []


class TestGrep:
    """Test grep content search."""

    def test_grep_basic(self, remote_fs: NexusFS) -> None:
        """Test basic grep search."""
        # Create test files with searchable content
        files = {
            "/file1.txt": b"Hello world\nTODO: fix this\nGoodbye",
            "/file2.txt": b"Another file\nNo todos here",
            "/file3.txt": b"TODO: implement feature",
        }

        # Store content for mocking reads
        content_store = {}

        # Mock router.route to return the backend
        from unittest.mock import Mock

        mock_route = Mock()
        mock_route.backend = remote_fs.backend
        mock_route.readonly = False
        remote_fs.router.route = Mock(return_value=mock_route)

        for path, content in files.items():
            content_hash = f"hash_{path}"
            content_store[content_hash] = content
            remote_fs.backend.write_content.return_value = content_hash
            remote_fs.write(path, content)

        # Mock read_content to return correct content based on hash
        def mock_read_content(content_hash, context=None):
            return content_store.get(content_hash, b"")

        remote_fs.backend.read_content.side_effect = mock_read_content

        # Search for "TODO"
        matches = remote_fs.grep("TODO")

        assert len(matches) == 2
        assert any(m["file"] == "/file1.txt" for m in matches)
        assert any(m["file"] == "/file3.txt" for m in matches)
        assert all("TODO" in m["content"] for m in matches)

    def test_grep_case_insensitive(self, remote_fs: NexusFS) -> None:
        """Test case-insensitive grep."""
        content = b"Error: something failed\nerror in line 2"
        path = "/test.txt"

        remote_fs.backend.write_content.return_value = "hash"
        remote_fs.write(path, content)
        remote_fs.backend.read_content.return_value = content

        # Case-insensitive search
        matches = remote_fs.grep("ERROR", ignore_case=True)

        assert len(matches) == 2

    def test_grep_with_file_pattern(self, remote_fs: NexusFS) -> None:
        """Test grep with file pattern filter."""
        files = {
            "/test.py": b"def test():\n    pass",
            "/main.py": b"def main():\n    pass",
            "/readme.txt": b"def should not match",
        }

        for path, content in files.items():
            remote_fs.backend.write_content.return_value = f"hash_{path}"
            remote_fs.write(path, content)
            remote_fs.backend.read_content.return_value = content

        # Search only in Python files
        matches = remote_fs.grep("def", file_pattern="*.py")

        assert len(matches) == 2
        assert all(m["file"].endswith(".py") for m in matches)


class TestDirectoryOperations:
    """Test directory operations."""

    def test_mkdir(self, remote_fs: NexusFS) -> None:
        """Test creating directory."""
        path = "/workspace/data"

        remote_fs.mkdir(path, parents=True, exist_ok=True)

        # Verify backend mkdir was called
        remote_fs.backend.mkdir.assert_called_once()

    def test_rmdir(self, remote_fs: NexusFS) -> None:
        """Test removing directory."""
        path = "/workspace/data"

        # Mock directory exists check
        remote_fs.backend.is_directory.return_value = True

        remote_fs.rmdir(path, recursive=True)

        # Verify backend rmdir was called
        remote_fs.backend.rmdir.assert_called_once()

    def test_is_directory(self, remote_fs: NexusFS) -> None:
        """Test checking if path is directory."""
        path = "/workspace"

        remote_fs.backend.is_directory.return_value = True
        assert remote_fs.is_directory(path) is True

        remote_fs.backend.is_directory.return_value = False
        assert remote_fs.is_directory(path) is False


class TestClose:
    """Test resource cleanup."""

    def test_close(self, temp_dir: Path, mock_gcs_backend: Mock) -> None:
        """Test that close releases resources."""
        db_path = temp_dir / "metadata.db"
        fs = NexusFS(backend=mock_gcs_backend, db_path=db_path)

        # Close should not raise
        fs.close()

        # Metadata store should be closed
        # (SQLAlchemy handles this internally)


class TestComputeEtag:
    """Test ETag computation."""

    def test_compute_etag(self, remote_fs: NexusFS) -> None:
        """Test ETag computation."""
        content = b"test content"
        etag = remote_fs._compute_etag(content)

        # Should be MD5 hash in hex
        assert len(etag) == 32
        # Should be deterministic
        assert etag == remote_fs._compute_etag(content)

        # Different content should produce different ETag
        etag2 = remote_fs._compute_etag(b"different content")
        assert etag != etag2


class TestReadOnlyPaths:
    """Test read-only path handling."""

    def test_write_to_readonly_path(self, remote_fs: NexusFS) -> None:
        """Test that writing to readonly path raises PermissionError."""
        # Mock readonly route
        from unittest.mock import Mock

        mock_route = Mock()
        mock_route.readonly = True
        remote_fs.router.route = Mock(return_value=mock_route)

        with pytest.raises(PermissionError) as exc_info:
            remote_fs.write("/readonly/file.txt", b"content")

        assert "read-only" in str(exc_info.value).lower()

    def test_delete_from_readonly_path(self, remote_fs: NexusFS) -> None:
        """Test that deleting from readonly path raises PermissionError."""
        # First create a file
        path = "/test.txt"
        content_hash = "hash123"
        remote_fs.backend.write_content.return_value = content_hash
        remote_fs.write(path, b"content")

        # Mock readonly route
        from unittest.mock import Mock

        mock_route = Mock()
        mock_route.readonly = True
        remote_fs.router.route = Mock(return_value=mock_route)

        with pytest.raises(PermissionError) as exc_info:
            remote_fs.delete(path)

        assert "read-only" in str(exc_info.value).lower()

    def test_mkdir_in_readonly_path(self, remote_fs: NexusFS) -> None:
        """Test that mkdir in readonly path raises PermissionError."""
        from unittest.mock import Mock

        mock_route = Mock()
        mock_route.readonly = True
        remote_fs.router.route = Mock(return_value=mock_route)

        with pytest.raises(PermissionError) as exc_info:
            remote_fs.mkdir("/readonly/dir", parents=True)

        assert "read-only" in str(exc_info.value).lower()

    def test_rmdir_in_readonly_path(self, remote_fs: NexusFS) -> None:
        """Test that rmdir in readonly path raises PermissionError."""
        from unittest.mock import Mock

        mock_route = Mock()
        mock_route.readonly = True
        remote_fs.router.route = Mock(return_value=mock_route)

        with pytest.raises(PermissionError) as exc_info:
            remote_fs.rmdir("/readonly/dir", recursive=True)

        assert "read-only" in str(exc_info.value).lower()


class TestRmdirWithFiles:
    """Test rmdir with files in directory."""

    def test_rmdir_non_empty_non_recursive(self, remote_fs: NexusFS) -> None:
        """Test that rmdir fails on non-empty directory without recursive flag."""
        import errno

        # Create file in directory
        path = "/workspace/data/file.txt"
        remote_fs.backend.write_content.return_value = "hash123"
        remote_fs.write(path, b"content")

        # Try to remove parent directory without recursive flag
        with pytest.raises(OSError) as exc_info:
            remote_fs.rmdir("/workspace/data", recursive=False)

        assert exc_info.value.errno == errno.ENOTEMPTY

    def test_rmdir_non_empty_recursive(self, remote_fs: NexusFS) -> None:
        """Test that rmdir succeeds on non-empty directory with recursive flag."""
        # Create files in directory
        files = [
            "/workspace/data/file1.txt",
            "/workspace/data/file2.txt",
            "/workspace/data/subdir/file3.txt",
        ]

        for file_path in files:
            remote_fs.backend.write_content.return_value = f"hash_{file_path}"
            remote_fs.write(file_path, b"content")

        # Remove directory recursively
        remote_fs.rmdir("/workspace/data", recursive=True)

        # All files should be deleted
        for file_path in files:
            assert not remote_fs.exists(file_path)


class TestGrepEdgeCases:
    """Test grep edge cases."""

    def test_grep_invalid_regex(self, remote_fs: NexusFS) -> None:
        """Test grep with invalid regex pattern."""
        with pytest.raises(ValueError) as exc_info:
            remote_fs.grep("[invalid")

        assert "Invalid regex pattern" in str(exc_info.value)

    def test_grep_max_results(self, remote_fs: NexusFS) -> None:
        """Test grep respects max_results limit."""
        # Create file with many matches
        content = b"\n".join([b"TODO: item " + str(i).encode() for i in range(100)])
        path = "/file.txt"
        content_hash = "hash123"

        remote_fs.backend.write_content.return_value = content_hash
        remote_fs.write(path, content)
        remote_fs.backend.read_content.return_value = content

        # Search with max_results limit
        matches = remote_fs.grep("TODO", max_results=10)

        assert len(matches) == 10

    def test_grep_binary_file(self, remote_fs: NexusFS) -> None:
        """Test grep skips binary files that can't be decoded as UTF-8."""
        # Create binary file with invalid UTF-8 sequences
        binary_content = b"\x80\x81\x82\x83\xff\xfe\xfd"
        path = "/binary.dat"
        content_hash = "hash123"

        remote_fs.backend.write_content.return_value = content_hash
        remote_fs.write(path, binary_content)
        remote_fs.backend.read_content.return_value = binary_content

        # Search should skip binary file (can't decode as UTF-8)
        matches = remote_fs.grep("TODO")

        # Binary file should be skipped, no matches
        assert len(matches) == 0


class TestListDeprecatedAPI:
    """Test backward compatibility with deprecated list API."""

    def test_list_with_prefix_parameter(self, remote_fs: NexusFS) -> None:
        """Test deprecated prefix parameter still works."""
        # Create files
        files = [
            "/workspace/file1.txt",
            "/workspace/file2.txt",
            "/other/file3.txt",
        ]

        for path in files:
            remote_fs.backend.write_content.return_value = f"hash_{path}"
            remote_fs.write(path, b"content")

        # Use deprecated prefix parameter
        files_list = remote_fs.list(prefix="/workspace")

        assert len(files_list) == 2
        assert "/workspace/file1.txt" in files_list
        assert "/workspace/file2.txt" in files_list
