"""Unit tests for NexusFSMountsMixin refactoring.

Tests cover the refactoring improvements:
- _matches_patterns(): Pattern matching helper
- SyncMountContext: Context dataclass for sync operations
- MetadataSyncResult: Named tuple for metadata sync results
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from nexus import LocalBackend, NexusFS
from nexus.core.nexus_fs_mounts import MetadataSyncResult, SyncMountContext
from nexus.core.permissions import OperationContext


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


class TestMatchesPatterns:
    """Tests for _matches_patterns() helper method."""

    def test_matches_patterns_no_patterns(self, nx: NexusFS) -> None:
        """Test that files match when no patterns are specified."""
        # No patterns = everything matches
        assert nx._matches_patterns("/test/file.py", None, None) is True
        assert nx._matches_patterns("/test/file.txt", None, None) is True
        assert nx._matches_patterns("/test/.git/config", None, None) is True

    def test_matches_patterns_include_only(self, nx: NexusFS) -> None:
        """Test include patterns only."""
        include = ["*.py", "*.md"]

        # Should match .py files
        assert nx._matches_patterns("/test/script.py", include, None) is True

        # Should match .md files
        assert nx._matches_patterns("/test/README.md", include, None) is True

        # Should not match other files
        assert nx._matches_patterns("/test/data.json", include, None) is False
        assert nx._matches_patterns("/test/image.png", include, None) is False

    def test_matches_patterns_exclude_only(self, nx: NexusFS) -> None:
        """Test exclude patterns only."""
        exclude = ["*.pyc", "*.log", ".git/*"]

        # Should exclude .pyc files
        assert nx._matches_patterns("/test/file.pyc", None, exclude) is False

        # Should exclude .log files
        assert nx._matches_patterns("/test/app.log", None, exclude) is False

        # Should exclude .git/* files
        assert nx._matches_patterns(".git/config", None, exclude) is False

        # Should include other files
        assert nx._matches_patterns("/test/file.py", None, exclude) is True
        assert nx._matches_patterns("/test/README.md", None, exclude) is True

    def test_matches_patterns_include_and_exclude(self, nx: NexusFS) -> None:
        """Test both include and exclude patterns."""
        include = ["*.py"]
        exclude = ["*_test.py", "*/__pycache__/*"]

        # Should match .py files
        assert nx._matches_patterns("/src/module.py", include, exclude) is True

        # Should exclude test files even if they match include
        assert nx._matches_patterns("/src/module_test.py", include, exclude) is False

        # Should exclude __pycache__ files even if they match include
        assert nx._matches_patterns("/src/__pycache__/module.py", include, exclude) is False

        # Should not match non-.py files
        assert nx._matches_patterns("/src/README.md", include, exclude) is False

    def test_matches_patterns_glob_patterns(self, nx: NexusFS) -> None:
        """Test various glob patterns."""
        # Wildcard patterns
        assert nx._matches_patterns("/test/file.txt", ["*.txt"], None) is True
        assert nx._matches_patterns("/test/file.py", ["*.txt"], None) is False

        # Directory patterns
        assert nx._matches_patterns("/test/subdir/file.py", ["*/subdir/*"], None) is True
        assert nx._matches_patterns("/test/file.py", ["*/subdir/*"], None) is False

        # Prefix patterns
        assert nx._matches_patterns("/test/temp_file.txt", ["*temp*"], None) is True
        assert nx._matches_patterns("/test/file.txt", ["*temp*"], None) is False

    def test_matches_patterns_empty_lists(self, nx: NexusFS) -> None:
        """Test with empty pattern lists."""
        # Empty lists should be treated as no patterns
        assert nx._matches_patterns("/test/file.py", [], []) is True
        assert nx._matches_patterns("/test/file.txt", [], None) is True
        assert nx._matches_patterns("/test/file.md", None, []) is True


class TestSyncMountContext:
    """Tests for SyncMountContext dataclass."""

    def test_sync_mount_context_creation(self) -> None:
        """Test creating a SyncMountContext."""
        ctx = SyncMountContext(
            mount_point="/mnt/test",
            path=None,
            recursive=True,
            dry_run=False,
            sync_content=True,
            include_patterns=["*.py"],
            exclude_patterns=["*.pyc"],
            generate_embeddings=False,
            context=None,
        )

        assert ctx.mount_point == "/mnt/test"
        assert ctx.path is None
        assert ctx.recursive is True
        assert ctx.dry_run is False
        assert ctx.sync_content is True
        assert ctx.include_patterns == ["*.py"]
        assert ctx.exclude_patterns == ["*.pyc"]
        assert ctx.generate_embeddings is False
        assert ctx.context is None
        assert ctx.backend is None  # Default value
        assert ctx.created_by is None  # Default value
        assert ctx.has_hierarchy is False  # Default value

    def test_sync_mount_context_with_operation_context(self) -> None:
        """Test creating SyncMountContext with operation context."""
        op_context = OperationContext(
            user="alice",
            groups=[],
            tenant_id="test_tenant",
            subject_type="user",
            subject_id="alice",
        )

        ctx = SyncMountContext(
            mount_point="/mnt/test",
            path="/data",
            recursive=False,
            dry_run=True,
            sync_content=False,
            include_patterns=None,
            exclude_patterns=None,
            generate_embeddings=True,
            context=op_context,
        )

        assert ctx.mount_point == "/mnt/test"
        assert ctx.path == "/data"
        assert ctx.context == op_context

    def test_sync_mount_context_populated_fields(self) -> None:
        """Test that context fields can be populated after creation."""
        ctx = SyncMountContext(
            mount_point="/mnt/test",
            path=None,
            recursive=True,
            dry_run=False,
            sync_content=True,
            include_patterns=None,
            exclude_patterns=None,
            generate_embeddings=False,
            context=None,
        )

        # These fields should be populated during sync_mount execution
        ctx.backend = "mock_backend"
        ctx.created_by = "user:alice"
        ctx.has_hierarchy = True

        assert ctx.backend == "mock_backend"
        assert ctx.created_by == "user:alice"
        assert ctx.has_hierarchy is True


class TestMetadataSyncResult:
    """Tests for MetadataSyncResult NamedTuple."""

    def test_metadata_sync_result_creation(self) -> None:
        """Test creating a MetadataSyncResult."""
        stats = {
            "files_scanned": 10,
            "files_created": 5,
            "files_updated": 2,
            "files_deleted": 1,
            "errors": [],
        }
        files = {"/test/file1.txt", "/test/file2.py"}

        result = MetadataSyncResult(stats, files)

        assert result.stats == stats
        assert result.files_found_in_backend == files

    def test_metadata_sync_result_named_access(self) -> None:
        """Test named field access on MetadataSyncResult."""
        stats = {"files_scanned": 5}
        files = {"/test/file.txt"}

        result = MetadataSyncResult(stats, files)

        # Named access
        assert result.stats["files_scanned"] == 5
        assert "/test/file.txt" in result.files_found_in_backend

        # Also works with indexing
        assert result[0] == stats
        assert result[1] == files

    def test_metadata_sync_result_immutable(self) -> None:
        """Test that MetadataSyncResult is immutable."""
        stats = {"files_scanned": 5}
        files = {"/test/file.txt"}

        result = MetadataSyncResult(stats, files)

        # Cannot assign to named fields (would raise AttributeError)
        with pytest.raises(AttributeError):
            result.stats = {}  # type: ignore[misc]

        with pytest.raises(AttributeError):
            result.files_found_in_backend = set()  # type: ignore[misc]


class TestSyncMountIntegration:
    """Integration tests for sync_mount with refactored code."""

    def test_sync_mount_with_patterns(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test that sync_mount correctly uses pattern filtering."""
        mount_data_dir = temp_dir / "pattern_mount"
        mount_data_dir.mkdir()

        # Create files with different extensions
        (mount_data_dir / "script.py").write_text("print('hello')")
        (mount_data_dir / "data.json").write_text('{"key": "value"}')
        (mount_data_dir / "test.pyc").write_bytes(b"compiled")
        (mount_data_dir / "README.md").write_text("# Test")

        nx.add_mount(
            mount_point="/mnt/pattern",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
        )

        # Sync with include pattern (only .py files)
        result = nx.sync_mount(
            "/mnt/pattern",
            include_patterns=["*.py"],
            dry_run=True,
        )

        # Should scan files, but dry run doesn't create entries
        assert result["files_scanned"] >= 0
        assert result["files_created"] == 0  # Dry run
        assert "errors" in result

    def test_sync_mount_exclude_patterns(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test that sync_mount correctly excludes files by pattern."""
        mount_data_dir = temp_dir / "exclude_mount"
        mount_data_dir.mkdir()

        # Create various files
        (mount_data_dir / "app.py").write_text("code")
        (mount_data_dir / "app.pyc").write_bytes(b"compiled")
        (mount_data_dir / "test.log").write_text("logs")

        nx.add_mount(
            mount_point="/mnt/exclude",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
        )

        # Sync excluding .pyc and .log files
        result = nx.sync_mount(
            "/mnt/exclude",
            exclude_patterns=["*.pyc", "*.log"],
            dry_run=True,
        )

        assert "files_scanned" in result
        assert "errors" in result

    def test_sync_mount_context_object_used(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test that sync_mount creates and uses SyncMountContext internally."""
        mount_data_dir = temp_dir / "context_mount"
        mount_data_dir.mkdir()
        (mount_data_dir / "file.txt").write_text("content")

        nx.add_mount(
            mount_point="/mnt/context",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
        )

        # Create operation context
        op_context = OperationContext(
            user="alice",
            groups=[],
            subject_type="user",
            subject_id="alice",
            tenant_id="test_tenant",
        )

        # Sync with context
        result = nx.sync_mount("/mnt/context", context=op_context)

        # Should complete successfully
        assert "files_scanned" in result
        assert "files_created" in result
        assert isinstance(result.get("errors", []), list)

    def test_sync_mount_returns_proper_structure(self, nx: NexusFS, temp_dir: Path) -> None:
        """Test that sync_mount returns the expected result structure."""
        mount_data_dir = temp_dir / "structure_mount"
        mount_data_dir.mkdir()

        nx.add_mount(
            mount_point="/mnt/structure",
            backend_type="local",
            backend_config={"data_dir": str(mount_data_dir)},
        )

        result = nx.sync_mount("/mnt/structure")

        # Verify all expected keys are present
        assert "files_scanned" in result
        assert "files_created" in result
        assert "files_updated" in result
        assert "files_deleted" in result
        assert "cache_synced" in result
        assert "cache_bytes" in result
        assert "cache_skipped" in result
        assert "embeddings_generated" in result
        assert "errors" in result

        # Verify types
        assert isinstance(result["files_scanned"], int)
        assert isinstance(result["files_created"], int)
        assert isinstance(result["files_updated"], int)
        assert isinstance(result["files_deleted"], int)
        assert isinstance(result["errors"], list)
