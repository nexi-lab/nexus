"""Unit tests for NexusFSMountsMixin refactoring.

Tests cover the refactoring improvements:
- _matches_patterns(): Pattern matching helper
"""

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from nexus import CASLocalBackend, NexusFS
from nexus.core.config import ParseConfig, PermissionConfig
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
        backend=CASLocalBackend(temp_dir),
        metadata_store=RaftMetadataStore.embedded(str(temp_dir / "raft-metadata")),
        record_store=SQLAlchemyRecordStore(db_path=temp_dir / "metadata.db"),
        parsing=ParseConfig(auto_parse=False),
        permissions=PermissionConfig(enforce=False),
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
