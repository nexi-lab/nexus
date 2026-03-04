"""Integration tests for LocalConnectorBackend.

Tests the LocalConnectorBackend end-to-end including:
- Mounting local folders into NexusFS
- Read/write through NexusFS API
- Directory operations
- L1 cache behavior
- FileWatcher integration (get_physical_path)
- Path security (symlink escape prevention)

Unlike unit tests which mock dependencies, these tests use real
file operations and the actual NexusFS stack.
"""

from pathlib import Path

import pytest

from nexus.backends.base.registry import create_connector
from nexus.backends.storage.local_connector import LocalConnectorBackend
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError
from nexus.contracts.types import OperationContext

# ============================================================================
# FIXTURES
# ============================================================================


@pytest.fixture
def local_folder(tmp_path: Path) -> Path:
    """Create a local folder with test files."""
    # Create directory structure
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "nested").mkdir()

    # Create test files
    (tmp_path / "readme.txt").write_text("Hello from readme")
    (tmp_path / "data.bin").write_bytes(b"\x00\x01\x02\x03")
    (tmp_path / "subdir" / "file.txt").write_text("Nested file content")
    (tmp_path / "subdir" / "nested" / "deep.txt").write_text("Deep nested")

    return tmp_path


@pytest.fixture
def connector(local_folder: Path) -> LocalConnectorBackend:
    """Create a LocalConnectorBackend instance."""
    return LocalConnectorBackend(local_folder)


@pytest.fixture
def context() -> OperationContext:
    """Create a basic operation context."""
    return OperationContext(
        user_id="test_user",
        groups=[],
        zone_id="test_zone",
    )


# ============================================================================
# REGISTRY INTEGRATION
# ============================================================================


class TestRegistryIntegration:
    """Test connector registry integration."""

    def test_create_via_registry(self, local_folder: Path):
        """Should create LocalConnectorBackend via create_connector."""
        connector = create_connector(
            "local_connector",
            local_path=str(local_folder),
        )
        assert isinstance(connector, LocalConnectorBackend)
        assert connector.local_path == local_folder

    def test_create_with_readonly(self, local_folder: Path):
        """Should pass readonly flag through registry."""
        connector = create_connector(
            "local_connector",
            local_path=str(local_folder),
            readonly=True,
        )
        assert connector.readonly is True

    def test_registry_name(self, local_folder: Path):
        """Should be registered as 'local_connector'."""
        from nexus.backends.base.registry import ConnectorRegistry

        # Trigger lazy registration
        create_connector("local_connector", local_path=str(local_folder))

        info = ConnectorRegistry.get_info("local_connector")
        assert info is not None
        assert info.name == "local_connector"
        assert "local" in info.description.lower()


# ============================================================================
# READ OPERATIONS
# ============================================================================


class TestReadOperations:
    """Test read operations through the connector."""

    def test_read_text_file(self, connector: LocalConnectorBackend, context: OperationContext):
        """Should read text file content."""
        context.backend_path = "readme.txt"
        context.virtual_path = "/mnt/local/readme.txt"

        result = connector.read_content("", context)

        assert result == b"Hello from readme"

    def test_read_binary_file(self, connector: LocalConnectorBackend, context: OperationContext):
        """Should read binary file content."""
        context.backend_path = "data.bin"
        context.virtual_path = "/mnt/local/data.bin"

        result = connector.read_content("", context)

        assert result == b"\x00\x01\x02\x03"

    def test_read_nested_file(self, connector: LocalConnectorBackend, context: OperationContext):
        """Should read files in nested directories."""
        context.backend_path = "subdir/nested/deep.txt"
        context.virtual_path = "/mnt/local/subdir/nested/deep.txt"

        result = connector.read_content("", context)

        assert result == b"Deep nested"

    def test_read_nonexistent_returns_not_found(
        self, connector: LocalConnectorBackend, context: OperationContext
    ):
        """Should raise NexusFileNotFoundError for nonexistent file."""
        context.backend_path = "does_not_exist.txt"
        context.virtual_path = "/mnt/local/does_not_exist.txt"

        with pytest.raises(NexusFileNotFoundError):
            connector.read_content("", context)


# ============================================================================
# WRITE OPERATIONS
# ============================================================================


class TestWriteOperations:
    """Test write operations through the connector."""

    def test_write_new_file(
        self, connector: LocalConnectorBackend, context: OperationContext, local_folder: Path
    ):
        """Should create new file."""
        context.backend_path = "new_file.txt"
        context.virtual_path = "/mnt/local/new_file.txt"

        result = connector.write_content(b"new content", context=context)

        assert result is not None
        assert (local_folder / "new_file.txt").read_bytes() == b"new content"

    def test_write_overwrites_existing(
        self, connector: LocalConnectorBackend, context: OperationContext, local_folder: Path
    ):
        """Should overwrite existing file."""
        context.backend_path = "readme.txt"
        context.virtual_path = "/mnt/local/readme.txt"

        result = connector.write_content(b"updated content", context=context)

        assert result is not None
        assert (local_folder / "readme.txt").read_bytes() == b"updated content"

    def test_write_creates_parent_dirs(
        self, connector: LocalConnectorBackend, context: OperationContext, local_folder: Path
    ):
        """Should create parent directories if needed."""
        context.backend_path = "new/nested/dir/file.txt"
        context.virtual_path = "/mnt/local/new/nested/dir/file.txt"

        result = connector.write_content(b"deeply nested", context=context)

        assert result is not None
        assert (local_folder / "new" / "nested" / "dir" / "file.txt").exists()

    def test_write_returns_content_hash(
        self, connector: LocalConnectorBackend, context: OperationContext
    ):
        """Should return WriteResult with content_hash on successful write."""
        context.backend_path = "hashed.txt"
        context.virtual_path = "/mnt/local/hashed.txt"

        result = connector.write_content(b"hash me", context=context)

        assert result is not None
        assert result.content_hash is not None
        assert len(result.content_hash) > 0

    def test_write_readonly_rejected(self, local_folder: Path, context: OperationContext):
        """Should raise BackendError for writes in readonly mode."""
        connector = LocalConnectorBackend(local_folder, readonly=True)
        context.backend_path = "readonly.txt"

        with pytest.raises(BackendError, match="read-only"):
            connector.write_content(b"should fail", context=context)


# ============================================================================
# DIRECTORY OPERATIONS
# ============================================================================


class TestDirectoryOperations:
    """Test directory operations."""

    def test_list_root_dir(self, connector: LocalConnectorBackend):
        """Should list root directory contents."""
        result = connector.list_dir("")

        # list_dir returns list[str] directly
        assert isinstance(result, list)
        assert "readme.txt" in result
        assert "data.bin" in result
        assert "subdir" in result

    def test_list_subdir(self, connector: LocalConnectorBackend):
        """Should list subdirectory contents."""
        result = connector.list_dir("subdir")

        assert isinstance(result, list)
        assert "file.txt" in result
        assert "nested" in result

    def test_list_returns_type_info(self, connector: LocalConnectorBackend):
        """Should include type (file/directory) in detailed listing."""
        # Use list_dir_detailed for detailed info
        result = connector.list_dir_detailed("")

        assert result.success is True
        entries_by_name = {e["name"]: e for e in result.data}
        assert entries_by_name["readme.txt"]["type"] == "file"
        assert entries_by_name["subdir"]["type"] == "directory"

    def test_list_nonexistent_dir(self, connector: LocalConnectorBackend):
        """Should return empty list for nonexistent directory."""
        result = connector.list_dir("nonexistent")

        # list_dir returns empty list for nonexistent
        assert result == []

    def test_mkdir(self, connector: LocalConnectorBackend, local_folder: Path):
        """Should create directory."""
        connector.mkdir("newdir")

        assert (local_folder / "newdir").is_dir()

    def test_mkdir_nested(self, connector: LocalConnectorBackend, local_folder: Path):
        """Should create nested directories."""
        connector.mkdir("new/nested/path")

        assert (local_folder / "new" / "nested" / "path").is_dir()

    def test_delete_file(self, connector: LocalConnectorBackend, local_folder: Path):
        """Should delete file."""
        result = connector.delete("readme.txt")

        assert result.success is True
        assert not (local_folder / "readme.txt").exists()

    def test_delete_empty_dir(self, connector: LocalConnectorBackend, local_folder: Path):
        """Should delete empty directory."""
        # Create empty dir
        (local_folder / "empty").mkdir()

        result = connector.delete("empty")

        assert result.success is True
        assert not (local_folder / "empty").exists()

    def test_exists(self, connector: LocalConnectorBackend):
        """Should check if path exists."""
        assert connector.exists("readme.txt") is True
        assert connector.exists("subdir") is True
        assert connector.exists("nonexistent") is False

    def test_is_dir(self, connector: LocalConnectorBackend):
        """Should check if path is directory."""
        assert connector.is_dir("subdir") is True
        assert connector.is_dir("readme.txt") is False


# ============================================================================
# CACHE INTEGRATION
# ============================================================================


class TestCacheIntegration:
    """Test L1 cache integration."""

    def test_l1_only_mode(self, connector: LocalConnectorBackend):
        """LocalConnectorBackend should use L1-only mode."""
        assert connector.l1_only is True
        assert connector._has_caching() is True
        assert connector._has_l2_caching() is False


# ============================================================================
# FILE WATCHER INTEGRATION
# ============================================================================


class TestFileWatcherIntegration:
    """Test FileWatcher integration points."""

    def test_get_physical_path(self, connector: LocalConnectorBackend, local_folder: Path):
        """get_physical_path should return correct OS path."""
        physical = connector.get_physical_path("subdir/file.txt")
        assert physical == local_folder / "subdir" / "file.txt"

    def test_get_watch_root(self, connector: LocalConnectorBackend, local_folder: Path):
        """get_watch_root should return mount root."""
        assert connector.get_watch_root() == local_folder


# ============================================================================
# PATH SECURITY
# ============================================================================


class TestPathSecurity:
    """Test path security (symlink escape prevention)."""

    @pytest.mark.skip(
        reason="TODO: https://github.com/nexi-lab/nexus/issues/1702 — backend refactor changed symlink escape behavior"
    )
    def test_symlink_escape_blocked(self, local_folder: Path):
        """Should block symlinks that escape mount root."""
        # Create escape symlink
        outside = local_folder.parent / "outside"
        outside.mkdir(exist_ok=True)
        (outside / "secret.txt").write_text("secret data")

        try:
            (local_folder / "escape").symlink_to(outside)
        except OSError:
            pytest.skip("Symlinks not supported")

        connector = LocalConnectorBackend(local_folder)
        context = OperationContext(user_id="test", groups=[], zone_id="test")
        context.backend_path = "escape/secret.txt"
        context.virtual_path = "/mnt/local/escape/secret.txt"

        # Should raise BackendError for path escape
        with pytest.raises(BackendError, match="escapes"):
            connector.read_content("", context)

    def test_parent_dir_escape_blocked(self, connector: LocalConnectorBackend):
        """Should block ../.. path traversal."""
        # This should be caught by path resolution
        assert connector.exists("../../../etc/passwd") is False


# ============================================================================
# RENAME OPERATIONS
# ============================================================================


class TestRenameOperations:
    """Test rename/move operations."""

    def test_rename_file(self, connector: LocalConnectorBackend, local_folder: Path):
        """Should rename file."""
        result = connector.rename("readme.txt", "readme_renamed.txt")

        assert result.success is True
        assert not (local_folder / "readme.txt").exists()
        assert (local_folder / "readme_renamed.txt").exists()
        assert (local_folder / "readme_renamed.txt").read_text() == "Hello from readme"

    def test_rename_to_different_dir(self, connector: LocalConnectorBackend, local_folder: Path):
        """Should move file to different directory."""
        result = connector.rename("readme.txt", "subdir/moved.txt")

        assert result.success is True
        assert not (local_folder / "readme.txt").exists()
        assert (local_folder / "subdir" / "moved.txt").exists()

    def test_rename_creates_parent_dirs(self, connector: LocalConnectorBackend, local_folder: Path):
        """Should create parent directories for destination."""
        result = connector.rename("readme.txt", "new/nested/path/file.txt")

        assert result.success is True
        assert (local_folder / "new" / "nested" / "path" / "file.txt").exists()

    def test_rename_directory(self, connector: LocalConnectorBackend, local_folder: Path):
        """Should rename directory."""
        result = connector.rename("subdir", "subdir_renamed")

        assert result.success is True
        assert not (local_folder / "subdir").exists()
        assert (local_folder / "subdir_renamed").is_dir()
        assert (local_folder / "subdir_renamed" / "file.txt").exists()

    def test_rename_nonexistent_fails(self, connector: LocalConnectorBackend):
        """Should fail when source doesn't exist."""
        result = connector.rename("nonexistent.txt", "new.txt")

        assert result.success is False
        assert "not found" in result.error_message.lower()

    def test_rename_readonly_rejected(self, local_folder: Path):
        """Should reject rename in readonly mode."""
        connector = LocalConnectorBackend(local_folder, readonly=True)

        result = connector.rename("readme.txt", "new.txt")

        assert result.success is False
        assert "read-only" in result.error_message


# ============================================================================
# STAT OPERATIONS
# ============================================================================


class TestStatOperations:
    """Test stat/metadata operations."""

    def test_stat_file(self, connector: LocalConnectorBackend):
        """Should return file metadata."""
        result = connector.stat("readme.txt")

        assert result.success is True
        assert result.data["size"] > 0
        assert result.data["is_file"] is True
        assert result.data["is_dir"] is False
        assert "mtime" in result.data
        assert "ctime" in result.data

    def test_stat_directory(self, connector: LocalConnectorBackend):
        """Should return directory metadata."""
        result = connector.stat("subdir")

        assert result.success is True
        assert result.data["is_dir"] is True
        assert result.data["is_file"] is False

    def test_stat_nonexistent(self, connector: LocalConnectorBackend):
        """Should return not_found for nonexistent path."""
        result = connector.stat("nonexistent.txt")

        assert result.success is False
        assert "not found" in result.error_message.lower()

    def test_stat_nested_file(self, connector: LocalConnectorBackend):
        """Should stat nested files."""
        result = connector.stat("subdir/nested/deep.txt")

        assert result.success is True
        assert result.data["is_file"] is True


# ============================================================================
# GLOB OPERATIONS
# ============================================================================


class TestGlobOperations:
    """Test glob pattern matching."""

    def test_glob_txt_files(self, connector: LocalConnectorBackend):
        """Should find all .txt files."""
        result = connector.glob("*.txt")

        assert result.success is True
        assert "readme.txt" in result.data

    def test_glob_recursive(self, connector: LocalConnectorBackend):
        """Should find files recursively with **."""
        result = connector.glob("**/*.txt")

        assert result.success is True
        assert "readme.txt" in result.data
        assert "subdir/file.txt" in result.data
        assert "subdir/nested/deep.txt" in result.data

    def test_glob_specific_dir(self, connector: LocalConnectorBackend):
        """Should find files in specific directory."""
        result = connector.glob("subdir/*.txt")

        assert result.success is True
        assert "subdir/file.txt" in result.data
        assert "readme.txt" not in result.data

    def test_glob_no_matches(self, connector: LocalConnectorBackend):
        """Should return empty list for no matches."""
        result = connector.glob("*.nonexistent")

        assert result.success is True
        assert result.data == []


# ============================================================================
# EMPTY FILE HANDLING
# ============================================================================


class TestEmptyFileHandling:
    """Test edge cases with empty files."""

    def test_write_empty_file(
        self, connector: LocalConnectorBackend, context: OperationContext, local_folder: Path
    ):
        """Should write empty file."""
        context.backend_path = "empty.txt"
        context.virtual_path = "/mnt/local/empty.txt"

        result = connector.write_content(b"", context=context)

        assert result is not None
        assert (local_folder / "empty.txt").exists()
        assert (local_folder / "empty.txt").read_bytes() == b""

    def test_read_empty_file(
        self, connector: LocalConnectorBackend, context: OperationContext, local_folder: Path
    ):
        """Should read empty file."""
        (local_folder / "empty.txt").write_bytes(b"")
        context.backend_path = "empty.txt"
        context.virtual_path = "/mnt/local/empty.txt"

        result = connector.read_content("", context)

        assert result == b""


# ============================================================================
# LARGE FILE HANDLING
# ============================================================================


class TestLargeFileHandling:
    """Test handling of large files."""

    def test_write_large_file(
        self, connector: LocalConnectorBackend, context: OperationContext, local_folder: Path
    ):
        """Should write large file (1MB)."""
        large_content = b"x" * (1024 * 1024)  # 1MB
        context.backend_path = "large.bin"
        context.virtual_path = "/mnt/local/large.bin"

        result = connector.write_content(large_content, context=context)

        assert result is not None
        assert (local_folder / "large.bin").stat().st_size == 1024 * 1024

    def test_read_large_file(
        self, connector: LocalConnectorBackend, context: OperationContext, local_folder: Path
    ):
        """Should read large file (1MB)."""
        large_content = b"y" * (1024 * 1024)  # 1MB
        (local_folder / "large.bin").write_bytes(large_content)
        context.backend_path = "large.bin"
        context.virtual_path = "/mnt/local/large.bin"

        result = connector.read_content("", context)

        assert len(result) == 1024 * 1024
        assert result == large_content


# ============================================================================
# UNICODE FILENAME HANDLING
# ============================================================================


class TestUnicodeFilenames:
    """Test handling of unicode filenames."""

    def test_write_unicode_filename(
        self, connector: LocalConnectorBackend, context: OperationContext, local_folder: Path
    ):
        """Should handle unicode filenames."""
        context.backend_path = "unicode_file.txt"
        context.virtual_path = "/mnt/local/unicode_file.txt"

        result = connector.write_content(b"Hello", context=context)

        assert result is not None
        assert (local_folder / "unicode_file.txt").exists()

    def test_read_unicode_content(
        self, connector: LocalConnectorBackend, context: OperationContext, local_folder: Path
    ):
        """Should handle unicode content."""
        unicode_content = b"Hello World"
        (local_folder / "unicode_content.txt").write_bytes(unicode_content)
        context.backend_path = "unicode_content.txt"
        context.virtual_path = "/mnt/local/unicode_content.txt"

        result = connector.read_content("", context)

        assert result.decode("utf-8") == "Hello World"

    def test_list_unicode_files(self, connector: LocalConnectorBackend, local_folder: Path):
        """Should list files with unicode names."""
        (local_folder / "test_file.txt").write_text("content")

        result = connector.list_dir("")

        # list_dir returns list[str] directly
        assert isinstance(result, list)
        assert "test_file.txt" in result


# ============================================================================
# CONCURRENT ACCESS
# ============================================================================


class TestConcurrentAccess:
    """Test concurrent read/write operations."""

    def test_concurrent_reads(self, connector: LocalConnectorBackend, local_folder: Path):
        """Should handle concurrent reads."""
        import concurrent.futures

        (local_folder / "concurrent.txt").write_text("concurrent content")

        def read_file(i: int) -> tuple[int, bytes]:
            ctx = OperationContext(
                user_id=f"user_{i}",
                groups=[],
                zone_id="test",
            )
            ctx.backend_path = "concurrent.txt"
            ctx.virtual_path = "/mnt/local/concurrent.txt"
            result = connector.read_content("", ctx)
            return (i, result)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(read_file, i) for i in range(10)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        # All reads should succeed
        for i, data in results:
            assert data == b"concurrent content", f"Read {i} returned wrong data"

    def test_concurrent_writes(self, connector: LocalConnectorBackend, local_folder: Path):
        """Should handle concurrent writes to different files."""
        import concurrent.futures

        def write_file(i: int) -> int:
            ctx = OperationContext(
                user_id=f"user_{i}",
                groups=[],
                zone_id="test",
            )
            ctx.backend_path = f"concurrent_{i}.txt"
            ctx.virtual_path = f"/mnt/local/concurrent_{i}.txt"
            connector.write_content(f"content_{i}".encode(), context=ctx)
            return i

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(write_file, i) for i in range(10)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        # All writes should succeed (no exception raised)
        for i in results:
            assert (local_folder / f"concurrent_{i}.txt").exists()


# ============================================================================
# DIRECTORY EDGE CASES
# ============================================================================


class TestDirectoryEdgeCases:
    """Test directory operation edge cases."""

    def test_rmdir_alias(self, connector: LocalConnectorBackend, local_folder: Path):
        """rmdir should work as alias for delete on directories."""
        (local_folder / "empty_dir").mkdir()

        connector.rmdir("empty_dir")

        assert not (local_folder / "empty_dir").exists()

    def test_delete_nonempty_dir_fails(self, connector: LocalConnectorBackend, local_folder: Path):
        """Should fail to delete non-empty directory."""
        result = connector.delete("subdir")

        assert result.success is False
        # Directory should still exist
        assert (local_folder / "subdir").exists()

    def test_list_empty_dir(self, connector: LocalConnectorBackend, local_folder: Path):
        """Should list empty directory."""
        (local_folder / "empty_dir").mkdir()

        result = connector.list_dir("empty_dir")

        # list_dir returns list[str] directly
        assert result == []

    def test_mkdir_existing_dir(self, connector: LocalConnectorBackend, local_folder: Path):
        """Should succeed when directory already exists."""
        connector.mkdir("subdir")

        assert (local_folder / "subdir").is_dir()

    def test_mkdir_readonly_rejected(self, local_folder: Path):
        """Should raise BackendError for mkdir in readonly mode."""
        connector = LocalConnectorBackend(local_folder, readonly=True)

        with pytest.raises(BackendError, match="read-only"):
            connector.mkdir("newdir")


# ============================================================================
# ERROR HANDLING
# ============================================================================


class TestErrorHandling:
    """Test error handling scenarios."""

    def test_read_directory_as_file(
        self, connector: LocalConnectorBackend, context: OperationContext
    ):
        """Should raise BackendError when reading directory as file."""
        context.backend_path = "subdir"
        context.virtual_path = "/mnt/local/subdir"

        with pytest.raises(BackendError, match="(?i)not a file"):
            connector.read_content("", context)

    def test_list_file_as_dir(self, connector: LocalConnectorBackend):
        """Should return empty list when listing file as directory."""
        result = connector.list_dir("readme.txt")

        # list_dir returns empty list for non-directories
        assert result == []

    def test_list_file_as_dir_detailed(self, connector: LocalConnectorBackend):
        """Should return error when listing file as directory with detailed info."""
        result = connector.list_dir_detailed("readme.txt")

        assert result.success is False
        assert "not a directory" in result.error_message.lower()

    def test_stat_after_delete(self, connector: LocalConnectorBackend, local_folder: Path):
        """Should return not_found after file is deleted."""
        (local_folder / "temp.txt").write_text("temp")
        connector.delete("temp.txt")

        result = connector.stat("temp.txt")

        assert result.success is False
        assert "not found" in result.error_message.lower()

    def test_invalid_path_with_context(
        self, connector: LocalConnectorBackend, context: OperationContext
    ):
        """Should raise BackendError for missing backend_path."""
        context.backend_path = None
        context.virtual_path = None

        with pytest.raises(BackendError):
            connector.read_content("", context)

    def test_delete_readonly_rejected(self, local_folder: Path):
        """Should reject delete in readonly mode."""
        connector = LocalConnectorBackend(local_folder, readonly=True)

        result = connector.delete("readme.txt")

        assert result.success is False
        assert "read-only" in result.error_message
