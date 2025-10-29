"""Test mkdir with parents=True behavior (like mkdir -p)."""

import tempfile
from pathlib import Path

import pytest

from nexus.backends.local import LocalBackend
from nexus.core.nexus_fs import NexusFS


def test_mkdir_parents_true_succeeds_if_exists():
    """Test that mkdir with parents=True succeeds if directory already exists (like mkdir -p)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root_path=tmpdir)
        db_path = Path(tmpdir) / "metadata.db"
        nx = NexusFS(backend=backend, db_path=db_path, enforce_permissions=False)

        # Create directory
        nx.mkdir("/workspace/foo/bar", parents=True)
        assert nx.is_directory("/workspace/foo/bar")

        # Calling again with parents=True should succeed (like mkdir -p)
        nx.mkdir("/workspace/foo/bar", parents=True)
        assert nx.is_directory("/workspace/foo/bar")

        # Also test with a parent directory
        nx.mkdir("/workspace/foo", parents=True)
        assert nx.is_directory("/workspace/foo")

        nx.close()


def test_mkdir_parents_false_fails_if_exists():
    """Test that mkdir without parents fails if directory already exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root_path=tmpdir)
        db_path = Path(tmpdir) / "metadata.db"
        nx = NexusFS(backend=backend, db_path=db_path, enforce_permissions=False)

        # Create directory
        nx.mkdir("/workspace/foo", parents=True)
        assert nx.is_directory("/workspace/foo")

        # Calling again without exist_ok should fail
        with pytest.raises(FileExistsError, match="Directory already exists"):
            nx.mkdir("/workspace/foo", parents=False, exist_ok=False)

        nx.close()


def test_mkdir_exist_ok_succeeds_if_exists():
    """Test that mkdir with exist_ok=True succeeds if directory already exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root_path=tmpdir)
        db_path = Path(tmpdir) / "metadata.db"
        nx = NexusFS(backend=backend, db_path=db_path, enforce_permissions=False)

        # Create directory
        nx.mkdir("/workspace/foo", parents=True)
        assert nx.is_directory("/workspace/foo")

        # Calling again with exist_ok=True should succeed
        nx.mkdir("/workspace/foo", parents=False, exist_ok=True)
        assert nx.is_directory("/workspace/foo")

        nx.close()


def test_mkdir_parents_creates_intermediate_dirs():
    """Test that mkdir with parents=True creates all intermediate directories."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root_path=tmpdir)
        db_path = Path(tmpdir) / "metadata.db"
        nx = NexusFS(backend=backend, db_path=db_path, enforce_permissions=False)

        # Create deep directory structure
        nx.mkdir("/workspace/a/b/c/d", parents=True)
        assert nx.is_directory("/workspace/a")
        assert nx.is_directory("/workspace/a/b")
        assert nx.is_directory("/workspace/a/b/c")
        assert nx.is_directory("/workspace/a/b/c/d")

        nx.close()


def test_mkdir_no_duplicate_entries_in_list():
    """Test that mkdir doesn't create duplicate entries in directory listings.

    This is a regression test for a bug where directories appeared twice in listings:
    once from the metadata entry (mime_type="inode/directory") and once from the
    backend directory marker.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root_path=tmpdir)
        db_path = Path(tmpdir) / "metadata.db"
        nx = NexusFS(backend=backend, db_path=db_path, enforce_permissions=False)

        # Create multiple directories
        nx.mkdir("/workspace/agent1", parents=True)
        nx.mkdir("/workspace/agent2", parents=True)
        nx.mkdir("/workspace/agent3", parents=True)

        # Create a file in one directory
        nx.write("/workspace/agent1/test.txt", b"Hello")

        # Test listing without details - should not have duplicates
        paths = nx.list("/workspace", recursive=False)
        assert len(paths) == 3, f"Expected 3 entries, got {len(paths)}: {paths}"
        assert "/workspace/agent1" in paths
        assert "/workspace/agent2" in paths
        assert "/workspace/agent3" in paths

        # Ensure each directory appears exactly once
        path_counts = {p: paths.count(p) for p in set(paths)}
        for path, count in path_counts.items():
            assert count == 1, f"Path {path} appears {count} times (should be 1)"

        # Test listing with details - should not have duplicates
        details = nx.list("/workspace", recursive=False, details=True)
        assert len(details) == 3, f"Expected 3 entries, got {len(details)}"

        # All entries should be marked as directories
        for entry in details:
            assert entry["is_directory"] is True, f"Entry {entry['path']} should be a directory"
            # Directory markers should have None mime_type (not "inode/directory")
            assert entry.get("mime_type") is None, (
                f"Directory {entry['path']} should have mime_type=None, got {entry.get('mime_type')}"
            )

        # Ensure each directory appears exactly once in details
        detail_paths = [d["path"] for d in details]
        detail_counts = {p: detail_paths.count(p) for p in set(detail_paths)}
        for path, count in detail_counts.items():
            assert count == 1, f"Path {path} appears {count} times in details (should be 1)"

        nx.close()
