"""Delegation round-trip tests for NexusFS → service forwarding.

Tests verify that NexusFS delegation methods correctly forward calls to
the underlying service instances with proper argument transformation.

Uses mock services (no Raft required) via object.__new__(NexusFS).

Covers:
- SearchService: 4 sync + 4 async (direct pass-through)

Note: VersionService, ReBACService, MCPService, SkillService, LLMService,
OAuthService, and ShareLinkService delegation was removed as part of
Issue #2033 (Strangler Fig decomposition). Those services are now accessed
directly via RPC register_service() / FastAPI Depends().
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.core.nexus_fs import NexusFS
from nexus.core.permissions import OperationContext

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
    fs.mcp_service = MagicMock()
    fs.skill_service = MagicMock()
    fs.llm_service = MagicMock()
    fs.oauth_service = MagicMock()
    fs.mount_service = MagicMock()
    fs.search_service = MagicMock()
    fs.share_link_service = MagicMock()
    return fs


@pytest.fixture
def context():
    """Standard operation context."""
    return OperationContext(
        user="test_user",
        groups=["test_group"],
        zone_id="test_zone",
        is_system=False,
        is_admin=False,
    )


# =============================================================================
# SearchService Delegation (4 sync + 4 async)
# =============================================================================


class TestSearchServiceDelegation:
    """Tests for NexusFS → SearchService delegation."""

    def test_list_delegates(self, mock_fs, context):
        """list forwards all args to search_service.list."""
        files = ["/a.txt", "/b.txt"]
        mock_fs.search_service.list = MagicMock(return_value=files)
        result = mock_fs.list(
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
        """asemantic_search forwards all args."""
        hits = [{"path": "/doc.txt", "score": 0.95}]
        mock_fs.search_service.semantic_search = AsyncMock(return_value=hits)
        result = asyncio.run(mock_fs.asemantic_search("find errors", path="/logs", limit=5))
        assert result == hits
        mock_fs.search_service.semantic_search.assert_called_once_with(
            query="find errors",
            path="/logs",
            limit=5,
            filters=None,
            search_mode="semantic",
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


