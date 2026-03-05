"""Delegation round-trip tests for NexusFS → service forwarding.

Tests verify that NexusFS delegation methods correctly forward calls to
the underlying service instances with proper argument transformation.

Uses mock services (no Raft required) via object.__new__(NexusFS).

Covers:
- ReBACService: 8 async methods (parameter renaming: zone_id→_zone_id)
- SkillService: direct service method calls (no __getattr__ compat)
- SearchService: 4 sync + 2 async (direct pass-through)
"""

from unittest.mock import MagicMock

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

    def test_glob_routed_via_getattr(self, mock_fs):
        """glob is routed to search_service via SERVICE_METHODS."""
        mock_fs.__dict__["search_service"] = svc = MagicMock()
        mock_fs.glob("*.py")
        svc.glob.assert_called_once_with("*.py")

    def test_glob_batch_routed_via_getattr(self, mock_fs):
        """glob_batch is routed to search_service via SERVICE_METHODS."""
        mock_fs.__dict__["search_service"] = svc = MagicMock()
        mock_fs.glob_batch(["*.py"])
        svc.glob_batch.assert_called_once_with(["*.py"])

    def test_grep_routed_via_getattr(self, mock_fs):
        """grep is routed to search_service via SERVICE_METHODS."""
        mock_fs.__dict__["search_service"] = svc = MagicMock()
        mock_fs.grep("pattern")
        svc.grep.assert_called_once_with("pattern")

    def test_search_service_glob_direct(self, mock_fs, context):
        """Callers should use search_service.glob() directly."""
        matches = ["/data/test.py"]
        mock_fs.search_service.glob = MagicMock(return_value=matches)
        result = mock_fs.search_service.glob("*.py", path="/data", context=context)
        assert result == matches
        mock_fs.search_service.glob.assert_called_once_with("*.py", path="/data", context=context)

    def test_search_service_grep_direct(self, mock_fs, context):
        """Callers should use search_service.grep() directly."""
        results = [{"path": "/a.py", "line": 1, "match": "import os"}]
        mock_fs.search_service.grep = MagicMock(return_value=results)
        result = mock_fs.search_service.grep("import os", path="/src", context=context)
        assert result == results
        mock_fs.search_service.grep.assert_called_once_with(
            "import os", path="/src", context=context
        )


# =============================================================================
# ShareLinkService Delegation (6 async methods)
# =============================================================================
