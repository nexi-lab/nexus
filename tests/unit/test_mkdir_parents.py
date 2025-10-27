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
