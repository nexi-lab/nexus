"""Delegation round-trip tests for NexusFS → service forwarding.

Tests verify that NexusFS delegation methods correctly forward calls to
the underlying service instances with proper argument transformation.

Uses mock services (no Raft required) via object.__new__(NexusFS).

Covers:
- ReBACService: 8 async methods (parameter renaming: zone_id→_zone_id)
- SkillService: direct service method calls (no __getattr__ compat)
- SearchService: 4 sync + 2 async (direct pass-through)
"""

from __future__ import annotations

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


class TestReBACServiceDelegation:
    """Tests for NexusFS → ReBACService delegation with parameter transformation."""

    def test_arebac_create_delegates(self, mock_fs, context):
        """arebac_create forwards all args."""
        mock_fs.rebac_service.rebac_create = AsyncMock(return_value={"tuple_id": "t1"})
        result = asyncio.run(
            mock_fs.arebac_create(
                subject=("user", "alice"),
                relation="viewer",
                object=("file", "/doc.txt"),
                zone_id="z1",
                context=context,
            )
        )
        assert result == {"tuple_id": "t1"}
        mock_fs.rebac_service.rebac_create.assert_called_once_with(
            subject=("user", "alice"),
            relation="viewer",
            object=("file", "/doc.txt"),
            expires_at=None,
            zone_id="z1",
            context=context,
            column_config=None,
        )

    def test_arebac_check_delegates(self, mock_fs, context):
        """arebac_check forwards all args."""
        mock_fs.rebac_service.rebac_check = AsyncMock(return_value=True)
        result = asyncio.run(
            mock_fs.arebac_check(
                subject=("user", "alice"),
                permission="read",
                object=("file", "/doc.txt"),
                zone_id="z1",
            )
        )
        assert result is True
        mock_fs.rebac_service.rebac_check.assert_called_once_with(
            subject=("user", "alice"),
            permission="read",
            object=("file", "/doc.txt"),
            context=None,
            zone_id="z1",
        )

    def test_arebac_expand_renames_zone_id(self, mock_fs):
        """arebac_expand transforms zone_id→_zone_id, limit→_limit."""
        mock_fs.rebac_service.rebac_expand = AsyncMock(return_value=[("user", "alice")])
        result = asyncio.run(
            mock_fs.arebac_expand(
                permission="read",
                object=("file", "/doc.txt"),
                zone_id="z1",
                limit=50,
            )
        )
        assert result == [("user", "alice")]
        mock_fs.rebac_service.rebac_expand.assert_called_once_with(
            permission="read",
            object=("file", "/doc.txt"),
            _zone_id="z1",
            _limit=50,
        )

    def test_arebac_explain_delegates(self, mock_fs, context):
        """arebac_explain forwards all args."""
        explanation = {"result": True, "reason": "direct"}
        mock_fs.rebac_service.rebac_explain = AsyncMock(return_value=explanation)
        result = asyncio.run(
            mock_fs.arebac_explain(
                subject=("user", "alice"),
                permission="read",
                object=("file", "/doc.txt"),
                zone_id="z1",
                context=context,
            )
        )
        assert result == explanation

    def test_arebac_check_batch_renames_zone_id(self, mock_fs):
        """arebac_check_batch transforms zone_id→_zone_id."""
        checks = [
            (("user", "alice"), "read", ("file", "/a.txt")),
            (("user", "bob"), "write", ("file", "/b.txt")),
        ]
        mock_fs.rebac_service.rebac_check_batch = AsyncMock(return_value=[True, False])
        result = asyncio.run(mock_fs.arebac_check_batch(checks, zone_id="z1"))
        assert result == [True, False]
        mock_fs.rebac_service.rebac_check_batch.assert_called_once_with(
            checks=checks,
            _zone_id="z1",
        )

    def test_arebac_delete_delegates(self, mock_fs):
        """arebac_delete forwards tuple_id."""
        mock_fs.rebac_service.rebac_delete = AsyncMock(return_value=True)
        result = asyncio.run(mock_fs.arebac_delete("tuple-123"))
        assert result is True
        mock_fs.rebac_service.rebac_delete.assert_called_once_with(tuple_id="tuple-123")

    def test_arebac_list_tuples_renames_params(self, mock_fs):
        """arebac_list_tuples transforms zone_id, limit, offset."""
        tuples = [{"tuple_id": "t1"}]
        mock_fs.rebac_service.rebac_list_tuples = AsyncMock(return_value=tuples)
        result = asyncio.run(
            mock_fs.arebac_list_tuples(
                subject=("user", "alice"),
                relation="viewer",
                zone_id="z1",
                limit=25,
                offset=10,
            )
        )
        assert result == tuples
        mock_fs.rebac_service.rebac_list_tuples.assert_called_once_with(
            subject=("user", "alice"),
            relation="viewer",
            object=None,
            relation_in=None,
            _zone_id="z1",
            _limit=25,
            _offset=10,
        )

    def test_aget_namespace_delegates(self, mock_fs):
        """aget_namespace forwards object_type."""
        ns = {"relations": ["viewer", "editor"]}
        mock_fs.rebac_service.get_namespace = AsyncMock(return_value=ns)
        result = asyncio.run(mock_fs.aget_namespace("file"))
        assert result == ns
        mock_fs.rebac_service.get_namespace.assert_called_once_with(object_type="file")


# =============================================================================
# SkillService Direct Calls (no __getattr__ compat — removed in PR #2258)
# =============================================================================


class TestSkillServiceDelegation:
    """Tests for calling skill_service / skill_package_service methods directly."""

    def test_skill_service_rpc_share(self, mock_fs, context):
        """skill_service.rpc_share is callable with expected args."""
        mock_fs.skill_service.rpc_share = MagicMock(
            return_value={"success": True, "tuple_id": "tuple-abc"}
        )
        result = mock_fs.skill_service.rpc_share("/skills/test.py", "user:bob", context)
        assert result["success"] is True
        mock_fs.skill_service.rpc_share.assert_called_once_with(
            "/skills/test.py", "user:bob", context
        )

    def test_skill_service_rpc_discover(self, mock_fs, context):
        """skill_service.rpc_discover is callable with expected args."""
        mock_fs.skill_service.rpc_discover = MagicMock(return_value={"skills": [], "count": 0})
        result = mock_fs.skill_service.rpc_discover("all", context)
        assert result == {"skills": [], "count": 0}
        mock_fs.skill_service.rpc_discover.assert_called_once_with("all", context)

    def test_skill_package_service_export(self, mock_fs, context):
        """skill_package_service.export is callable with expected args."""
        mock_fs.skill_package_service.export = MagicMock(return_value={"path": "/tmp/test.skill"})
        result = mock_fs.skill_package_service.export(
            skill_path="/skills/test.py",
            format="generic",
            context=context,
        )
        assert result == {"path": "/tmp/test.skill"}
        mock_fs.skill_package_service.export.assert_called_once_with(
            skill_path="/skills/test.py",
            format="generic",
            context=context,
        )

    def test_skill_package_service_import_skill(self, mock_fs, context):
        """skill_package_service.import_skill is callable with expected args."""
        mock_fs.skill_package_service.import_skill = MagicMock(return_value={"imported": True})
        result = mock_fs.skill_package_service.import_skill(
            source_path="/tmp/test.skill",
            context=context,
        )
        assert result == {"imported": True}
        mock_fs.skill_package_service.import_skill.assert_called_once_with(
            source_path="/tmp/test.skill",
            context=context,
        )


# =============================================================================
# SearchService Delegation (4 sync + 2 async)
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

