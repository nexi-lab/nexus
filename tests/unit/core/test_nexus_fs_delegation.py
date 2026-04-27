"""Delegation round-trip tests for NexusFS kernel syscalls and service access (Issue #1452).

Tests verify that:
- NexusFS kernel syscalls (sys_readdir) use internal metadata directly
- Services are accessed via ServiceRegistry (nx.service("name"))

Uses mock internals (no Raft required) via object.__new__(NexusFS).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.types import OperationContext
from nexus.core.nexus_fs import NexusFS

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_fs():
    """Create a NexusFS with mock internals, bypassing __init__.

    Uses MagicMock for kernel components and mock _kernel for services.
    """
    fs = object.__new__(NexusFS)
    fs.version_service = MagicMock()
    fs.skill_service = MagicMock()
    fs.skill_package_service = MagicMock()
    fs.metadata = MagicMock()
    mock_rebac = MagicMock()
    mock_search = MagicMock()
    fs._kernel = MagicMock()
    fs._kernel.service_lookup = MagicMock(
        side_effect=lambda name: {
            "rebac": mock_rebac,
            "search": mock_search,
        }.get(name)
    )
    # readdir fallback to metadata.list_iter for non-Rust paths
    fs._kernel.readdir = MagicMock(side_effect=ValueError("test fallback"))
    # Ensure route() returns is_external=False so external intercept doesn't fire
    mock_route = MagicMock()
    mock_route.is_external = False
    fs._kernel.route = MagicMock(return_value=mock_route)
    fs._hook_specs = {}
    fs._zone_id = ROOT_ZONE_ID
    return fs


@pytest.fixture
def context():
    """Standard operation context.

    Uses ROOT_ZONE_ID so the sys_readdir zone-column filter (Issue #3779
    follow-up) doesn't apply — these tests exercise delegation to
    metadata, not zone isolation.
    """
    from nexus.contracts.constants import ROOT_ZONE_ID

    return OperationContext(
        user_id="test_user",
        groups=["test_group"],
        zone_id=ROOT_ZONE_ID,
        is_system=False,
        is_admin=False,
    )


# =============================================================================
# sys_readdir — kernel uses metadata directly (Phase 2b)
# =============================================================================


class TestSysReaddir:
    """Tests for NexusFS.sys_readdir using kernel metadata directly."""

    @pytest.mark.asyncio
    def test_sys_readdir_uses_metadata(self, mock_fs, context):
        """sys_readdir calls self.metadata.list_iter() — no SearchService delegation."""
        entry1 = SimpleNamespace(path="/data/a.txt", size=10, content_id="e1")
        entry2 = SimpleNamespace(path="/data/b.txt", size=20, content_id="e2")
        mock_fs.metadata.list_iter = MagicMock(return_value=iter([entry1, entry2]))

        result = mock_fs.sys_readdir(path="/data", recursive=False, context=context)

        assert result == ["/data/a.txt", "/data/b.txt"]
        mock_fs.metadata.list_iter.assert_called_once_with(prefix="/data/", recursive=False)

    @pytest.mark.asyncio
    def test_sys_readdir_details(self, mock_fs, context):
        """sys_readdir with details=True returns dicts from metadata."""
        entry = SimpleNamespace(
            path="/data/a.txt",
            size=42,
            content_id="abc",
            entry_type=0,
            zone_id=ROOT_ZONE_ID,
            owner_id=None,
            modified_at=None,
            version=1,
        )
        mock_fs.metadata.list_iter = MagicMock(return_value=iter([entry]))
        mock_fs.metadata.is_implicit_directory = MagicMock(return_value=False)

        result = mock_fs.sys_readdir(path="/data", details=True, context=context)

        assert result == [
            {
                "path": "/data/a.txt",
                "size": 42,
                "content_id": "abc",
                "entry_type": 0,
                "zone_id": "root",
                "owner_id": None,
                "modified_at": None,
                "version": 1,
            }
        ]

    @pytest.mark.asyncio
    def test_sys_readdir_root_prefix(self, mock_fs, context):
        """sys_readdir with path='/' uses empty prefix."""
        mock_fs.metadata.list_iter = MagicMock(return_value=iter([]))

        mock_fs.sys_readdir(path="/", context=context)

        mock_fs.metadata.list_iter.assert_called_once_with(prefix="", recursive=True)


# =============================================================================
# Service access via ServiceRegistry
# =============================================================================


class TestServiceAccess:
    """Tests for accessing services via nx.service("name")."""

    def test_search_service_glob_direct(self, mock_fs, context):
        """Callers should use service("search").glob() directly."""
        matches = ["/data/test.py"]
        mock_glob = MagicMock(return_value=matches)
        mock_fs.service("search").glob = mock_glob
        result = mock_fs.service("search").glob("*.py", path="/data", context=context)
        assert result == matches
        mock_glob.assert_called_once_with("*.py", path="/data", context=context)

    def test_search_service_grep_direct(self, mock_fs, context):
        """Callers should use service("search").grep() directly."""
        results = [{"path": "/a.py", "line": 1, "match": "import os"}]
        mock_grep = MagicMock(return_value=results)
        mock_fs.service("search").grep = mock_grep
        result = mock_fs.service("search").grep("import os", path="/src", context=context)
        assert result == results
        mock_grep.assert_called_once_with("import os", path="/src", context=context)


# =============================================================================
# ShareLinkService Delegation (6 async methods)
# =============================================================================
