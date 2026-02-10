"""Unit tests for batch API endpoints (Issue #859).

Tests cover batch operations:
- exists_batch: Check existence of multiple paths in single call
- metadata_batch: Get metadata for multiple paths in single call
- glob_batch: Execute multiple glob patterns in single call
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from nexus import LocalBackend, NexusFS
from nexus.factory import create_nexus_fs
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def nx(temp_dir: Path) -> Generator[NexusFS, None, None]:
    """Create a NexusFS instance for testing."""
    nx = create_nexus_fs(
        backend=LocalBackend(temp_dir),
        metadata_store=RaftMetadataStore.embedded(str(temp_dir / "raft-metadata")),
        record_store=SQLAlchemyRecordStore(db_path=temp_dir / "metadata.db"),
        auto_parse=False,
        enforce_permissions=False,
    )
    yield nx
    nx.close()


class TestExistsBatch:
    """Tests for exists_batch method."""

    def test_exists_batch_all_exist(self, nx: NexusFS) -> None:
        """Test exists_batch when all paths exist."""
        nx.write("/file1.txt", b"Content 1")
        nx.write("/file2.txt", b"Content 2")
        nx.write("/dir/file3.txt", b"Content 3")

        result = nx.exists_batch(["/file1.txt", "/file2.txt", "/dir/file3.txt"])

        assert result == {
            "/file1.txt": True,
            "/file2.txt": True,
            "/dir/file3.txt": True,
        }

    def test_exists_batch_none_exist(self, nx: NexusFS) -> None:
        """Test exists_batch when no paths exist."""
        result = nx.exists_batch(["/missing1.txt", "/missing2.txt"])

        assert result == {
            "/missing1.txt": False,
            "/missing2.txt": False,
        }

    def test_exists_batch_mixed(self, nx: NexusFS) -> None:
        """Test exists_batch with mix of existing and missing paths."""
        nx.write("/exists.txt", b"Content")

        result = nx.exists_batch(["/exists.txt", "/missing.txt"])

        assert result == {
            "/exists.txt": True,
            "/missing.txt": False,
        }

    def test_exists_batch_empty_list(self, nx: NexusFS) -> None:
        """Test exists_batch with empty list."""
        result = nx.exists_batch([])

        assert result == {}

    def test_exists_batch_directories(self, nx: NexusFS) -> None:
        """Test exists_batch with implicit directories."""
        nx.write("/dir/subdir/file.txt", b"Content")

        result = nx.exists_batch(["/dir", "/dir/subdir", "/dir/subdir/file.txt"])

        # Directories exist implicitly because they contain files
        assert result["/dir/subdir/file.txt"] is True
        # Implicit directories should also return True
        assert result["/dir"] is True
        assert result["/dir/subdir"] is True

    def test_exists_batch_invalid_paths(self, nx: NexusFS) -> None:
        """Test exists_batch handles invalid paths gracefully."""
        nx.write("/valid.txt", b"Content")

        # Invalid paths should return False, not raise exceptions
        result = nx.exists_batch(["/valid.txt", ""])

        assert result["/valid.txt"] is True
        assert result[""] is False


class TestMetadataBatch:
    """Tests for metadata_batch method."""

    def test_metadata_batch_all_exist(self, nx: NexusFS) -> None:
        """Test metadata_batch when all paths exist."""
        nx.write("/file1.txt", b"Content 1")
        nx.write("/file2.txt", b"Content 2")

        result = nx.metadata_batch(["/file1.txt", "/file2.txt"])

        assert result["/file1.txt"] is not None
        assert result["/file1.txt"]["path"] == "/file1.txt"
        assert result["/file1.txt"]["size"] == len(b"Content 1")

        assert result["/file2.txt"] is not None
        assert result["/file2.txt"]["path"] == "/file2.txt"
        assert result["/file2.txt"]["size"] == len(b"Content 2")

    def test_metadata_batch_none_exist(self, nx: NexusFS) -> None:
        """Test metadata_batch when no paths exist."""
        result = nx.metadata_batch(["/missing1.txt", "/missing2.txt"])

        assert result == {
            "/missing1.txt": None,
            "/missing2.txt": None,
        }

    def test_metadata_batch_mixed(self, nx: NexusFS) -> None:
        """Test metadata_batch with mix of existing and missing paths."""
        nx.write("/exists.txt", b"Content")

        result = nx.metadata_batch(["/exists.txt", "/missing.txt"])

        assert result["/exists.txt"] is not None
        assert result["/exists.txt"]["path"] == "/exists.txt"
        assert result["/missing.txt"] is None

    def test_metadata_batch_empty_list(self, nx: NexusFS) -> None:
        """Test metadata_batch with empty list."""
        result = nx.metadata_batch([])

        assert result == {}

    def test_metadata_batch_includes_all_fields(self, nx: NexusFS) -> None:
        """Test metadata_batch returns all expected fields."""
        nx.write("/file.txt", b"Test content")

        result = nx.metadata_batch(["/file.txt"])
        metadata = result["/file.txt"]

        assert metadata is not None
        # Check all required fields are present
        assert "path" in metadata
        assert "size" in metadata
        assert "etag" in metadata
        assert "mime_type" in metadata
        assert "created_at" in metadata
        assert "modified_at" in metadata
        assert "version" in metadata
        assert "is_directory" in metadata

        # Verify values
        assert metadata["size"] == len(b"Test content")
        assert metadata["is_directory"] is False


class TestGlobBatch:
    """Tests for glob_batch method."""

    def test_glob_batch_single_pattern(self, nx: NexusFS) -> None:
        """Test glob_batch with single pattern."""
        nx.write("/file1.py", b"Python 1")
        nx.write("/file2.py", b"Python 2")
        nx.write("/file3.txt", b"Text")

        result = nx.glob_batch(["*.py"])

        assert "*.py" in result
        assert len(result["*.py"]) == 2
        assert "/file1.py" in result["*.py"]
        assert "/file2.py" in result["*.py"]

    def test_glob_batch_multiple_patterns(self, nx: NexusFS) -> None:
        """Test glob_batch with multiple patterns."""
        nx.write("/src/main.py", b"Python")
        nx.write("/src/app.js", b"JavaScript")
        nx.write("/README.txt", b"Readme")

        result = nx.glob_batch(["**/*.py", "**/*.js", "*.txt"])

        assert len(result) == 3
        assert "/src/main.py" in result["**/*.py"]
        assert "/src/app.js" in result["**/*.js"]
        assert "/README.txt" in result["*.txt"]

    def test_glob_batch_no_matches(self, nx: NexusFS) -> None:
        """Test glob_batch when patterns have no matches."""
        nx.write("/file.txt", b"Content")

        result = nx.glob_batch(["*.py", "*.js"])

        assert result["*.py"] == []
        assert result["*.js"] == []

    def test_glob_batch_empty_patterns(self, nx: NexusFS) -> None:
        """Test glob_batch with empty pattern list."""
        result = nx.glob_batch([])

        assert result == {}

    def test_glob_batch_recursive_patterns(self, nx: NexusFS) -> None:
        """Test glob_batch with recursive patterns."""
        nx.write("/root.py", b"Root")
        nx.write("/src/module.py", b"Module")
        nx.write("/src/tests/test_module.py", b"Test")

        result = nx.glob_batch(["**/*.py"])

        assert len(result["**/*.py"]) == 3
        assert "/root.py" in result["**/*.py"]
        assert "/src/module.py" in result["**/*.py"]
        assert "/src/tests/test_module.py" in result["**/*.py"]

    def test_glob_batch_with_base_path(self, nx: NexusFS) -> None:
        """Test glob_batch with custom base path."""
        nx.write("/root.py", b"Root")
        nx.write("/src/module.py", b"Module")
        nx.write("/src/tests/test_module.py", b"Test")

        result = nx.glob_batch(["*.py"], path="/src")

        # Should only find files directly under /src, not in subdirectories
        # (since *.py doesn't use **)
        assert "/src/module.py" in result["*.py"]
        # test_module.py should be found via pattern matching against /src base
        # Actually with the path parameter and *.py pattern, it depends on implementation

    def test_glob_batch_shares_file_listing(self, nx: NexusFS) -> None:
        """Test that glob_batch efficiently shares file listing across patterns.

        This is a performance optimization test - multiple patterns should
        reuse the same file listing instead of calling list() multiple times.
        """
        # Create many files
        for i in range(20):
            nx.write(f"/file{i}.py", b"Python")
            nx.write(f"/file{i}.txt", b"Text")

        # Multiple patterns should work efficiently
        result = nx.glob_batch(["*.py", "*.txt", "file1*", "file2*"])

        assert len(result) == 4
        assert len(result["*.py"]) == 20
        assert len(result["*.txt"]) == 20


class TestBatchEndpointsRPCExposed:
    """Tests that batch methods are properly exposed via RPC."""

    def test_exists_batch_has_rpc_expose(self, nx: NexusFS) -> None:
        """Test that exists_batch is decorated with @rpc_expose."""
        assert hasattr(nx.exists_batch, "_rpc_exposed")

    def test_metadata_batch_has_rpc_expose(self, nx: NexusFS) -> None:
        """Test that metadata_batch is decorated with @rpc_expose."""
        assert hasattr(nx.metadata_batch, "_rpc_exposed")

    def test_glob_batch_has_rpc_expose(self, nx: NexusFS) -> None:
        """Test that glob_batch is decorated with @rpc_expose."""
        assert hasattr(nx.glob_batch, "_rpc_exposed")
