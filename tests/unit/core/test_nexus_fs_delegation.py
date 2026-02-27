"""Delegation round-trip tests for NexusFS → service forwarding.

Tests verify that NexusFS delegation methods correctly forward calls to
the underlying service instances with proper argument transformation.

Uses mock services (no Raft required) via object.__new__(NexusFS).

Covers:
- ReBACService: 8 async methods (parameter renaming: zone_id→_zone_id)
- SkillService: direct service method calls (no __getattr__ compat)
- SearchService: 4 sync + 2 async (direct pass-through)
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.contracts.types import OperationContext
from nexus.core.nexus_fs import NexusFS

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_fs():
    """Create a NexusFS with mock services, bypassing __init__.

    Uses MagicMock for all services. Individual tests set AsyncMock
    on specific methods they need to await.
    """
    fs = object.__new__(NexusFS)
    fs.version_service = MagicMock()
    fs.rebac_service = MagicMock()
    fs.skill_service = MagicMock()
    fs.skill_package_service = MagicMock()
    fs.search_service = MagicMock()
    return fs


@pytest.fixture
def context():
    """Standard operation context."""
    return OperationContext(
        user_id="test_user",
        groups=["test_group"],
        zone_id="test_zone",
        is_system=False,
        is_admin=False,
    )


# =============================================================================
# VersionService Delegation (4 async methods)
# =============================================================================


class TestVersionServiceDelegation:
    """Tests for NexusFS → VersionService delegation."""

    def test_aget_version_delegates(self, mock_fs, context):
        """aget_version forwards path, version, context."""
        mock_fs.version_service.get_version = AsyncMock(return_value=b"v1data")
        result = asyncio.run(mock_fs.aget_version("/file.txt", 1, context))
        assert result == b"v1data"
        mock_fs.version_service.get_version.assert_called_once_with("/file.txt", 1, context)

    def test_alist_versions_delegates(self, mock_fs, context):
        """alist_versions forwards path and context."""
        versions = [{"version": 1}, {"version": 2}]
        mock_fs.version_service.list_versions = AsyncMock(return_value=versions)
        result = asyncio.run(mock_fs.alist_versions("/file.txt", context))
        assert result == versions
        mock_fs.version_service.list_versions.assert_called_once_with("/file.txt", context)

    def test_arollback_delegates(self, mock_fs, context):
        """arollback forwards path, version, context."""
        mock_fs.version_service.rollback = AsyncMock(return_value=None)
        asyncio.run(mock_fs.arollback("/file.txt", 2, context))
        mock_fs.version_service.rollback.assert_called_once_with("/file.txt", 2, context)

    def test_adiff_versions_delegates(self, mock_fs, context):
        """adiff_versions forwards path, v1, v2, mode, context."""
        diff = {"changed": True}
        mock_fs.version_service.diff_versions = AsyncMock(return_value=diff)
        result = asyncio.run(mock_fs.adiff_versions("/file.txt", 1, 2, "content", context))
        assert result == diff
        mock_fs.version_service.diff_versions.assert_called_once_with(
            "/file.txt", 1, 2, "content", context
        )

    def test_adiff_versions_default_mode(self, mock_fs):
        """adiff_versions forwards to version_service.diff_versions directly."""
        mock_fs.version_service.diff_versions = AsyncMock(return_value={})
        asyncio.run(mock_fs.adiff_versions("/file.txt", 1, 2))
        # __getattr__ alias passes args through; defaults are on the service method
        mock_fs.version_service.diff_versions.assert_called_once_with("/file.txt", 1, 2)


# =============================================================================
# ReBACService Delegation (8 async methods with parameter renaming)
# =============================================================================


class TestSearchServiceDelegation:
    """Tests for NexusFS → SearchService delegation."""

    def test_list_delegates(self, mock_fs, context):
        """list forwards all args to search_service.list."""
        files = ["/a.txt", "/b.txt"]
        mock_fs.search_service.list = MagicMock(return_value=files)
        result = mock_fs.sys_readdir(
            path="/data",
            recursive=False,
            details=True,
            context=context,
        )
        assert result == files
        mock_fs.search_service.list.assert_called_once_with(
            path="/data",
            recursive=False,
            details=True,
            show_parsed=True,
            context=context,
            limit=None,
            cursor=None,
        )

    def test_glob_delegates(self, mock_fs, context):
        """glob forwards pattern, path, context."""
        matches = ["/data/test.py"]
        mock_fs.search_service.glob = MagicMock(return_value=matches)
        result = mock_fs.glob("*.py", path="/data", context=context)
        assert result == matches
        mock_fs.search_service.glob.assert_called_once_with(
            pattern="*.py", path="/data", context=context
        )

    def test_glob_batch_delegates(self, mock_fs, context):
        """glob_batch forwards patterns, path, context."""
        batch = {"*.py": ["/a.py"], "*.txt": ["/b.txt"]}
        mock_fs.search_service.glob_batch = MagicMock(return_value=batch)
        result = mock_fs.glob_batch(["*.py", "*.txt"], context=context)
        assert result == batch

    def test_grep_delegates(self, mock_fs, context):
        """grep forwards all args."""
        results = [{"path": "/a.py", "line": 1, "match": "import os"}]
        mock_fs.search_service.grep = MagicMock(return_value=results)
        result = mock_fs.grep(
            "import os",
            path="/src",
            ignore_case=True,
            context=context,
        )
        assert result == results
        mock_fs.search_service.grep.assert_called_once_with(
            pattern="import os",
            path="/src",
            file_pattern=None,
            ignore_case=True,
            max_results=100,
            search_mode="auto",
            context=context,
        )

    def test_asemantic_search_delegates(self, mock_fs):
        """asemantic_search forwards all args to search_service.semantic_search."""
        hits = [{"path": "/doc.txt", "score": 0.95}]
        mock_fs.search_service.semantic_search = AsyncMock(return_value=hits)
        result = asyncio.run(mock_fs.asemantic_search("find errors", path="/logs", limit=5))
        assert result == hits
        # __getattr__ pass-through: args forwarded as-is, service handles defaults
        mock_fs.search_service.semantic_search.assert_called_once_with(
            "find errors",
            path="/logs",
            limit=5,
        )

    def test_asemantic_search_index_delegates(self, mock_fs):
        """asemantic_search_index forwards path and recursive."""
        stats = {"indexed": 42}
        mock_fs.search_service.semantic_search_index = AsyncMock(return_value=stats)
        result = asyncio.run(mock_fs.asemantic_search_index(path="/data", recursive=False))
        assert result == stats


# =============================================================================
# ShareLinkService Delegation (6 async methods)
# =============================================================================
