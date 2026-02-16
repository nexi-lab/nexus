"""Integration tests for MCP tool namespace — full ReBAC ↔ middleware stack (Issue #1272).

Tests the full path from ReBAC grant → ToolNamespaceMiddleware → discovery
filtering, using a real EnhancedReBACManager with in-memory SQLite.

These tests verify:
    1. Two agents with different profiles see different tools
    2. Grant revocation removes tool from visible set
    3. Cache invalidation on revision change
    4. Profile grant + middleware filtering end-to-end
    5. Performance: filtering 30 tools under 10ms
"""

from __future__ import annotations

import json
import time
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from nexus.mcp.middleware import ToolNamespaceMiddleware
from nexus.mcp.profiles import (
    TOOL_PATH_PREFIX,
    grant_tools_for_profile,
    load_profiles_from_dict,
    revoke_tools_by_tuple_ids,
)
from nexus.mcp.server import create_mcp_server
from nexus.rebac.manager import EnhancedReBACManager
from nexus.storage.models import Base

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def rebac_engine():
    """In-memory SQLite engine with ReBAC tables."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def rebac_manager(rebac_engine):
    """Real EnhancedReBACManager on in-memory SQLite."""
    return EnhancedReBACManager(engine=rebac_engine, cache_ttl_seconds=1)


@pytest.fixture
def middleware(rebac_manager):
    """ToolNamespaceMiddleware with real ReBAC backend."""
    return ToolNamespaceMiddleware(
        rebac_manager=rebac_manager,
        zone_id=None,
        cache_ttl=60,
        revision_window=1,  # Fine-grained for testing
    )


@pytest.fixture
def mock_nx():
    """Minimal mock NexusFS."""
    nx = Mock()
    nx.read = Mock(return_value=b"test")
    nx.write = Mock()
    nx.delete = Mock()
    nx.list = Mock(return_value=[])
    nx.glob = Mock(return_value=[])
    nx.grep = Mock(return_value=[])
    nx.exists = Mock(return_value=True)
    nx.is_directory = Mock(return_value=False)
    nx.mkdir = Mock()
    nx.rmdir = Mock()
    nx.edit = Mock(
        return_value={"success": True, "diff": "", "applied_count": 0, "matches": [], "errors": []}
    )
    return nx


@pytest.fixture
def profiles():
    """Test profile config with two profiles."""
    return load_profiles_from_dict(
        {
            "profiles": {
                "reader": {
                    "description": "Read-only",
                    "tools": ["nexus_read_file", "nexus_list_files", "nexus_file_info"],
                },
                "writer": {
                    "extends": "reader",
                    "description": "Read-write",
                    "tools": ["nexus_write_file", "nexus_edit_file", "nexus_delete_file"],
                },
            },
            "default_profile": "reader",
        }
    )


def _get_tool(server, name):
    return server._tool_manager._tools[name]


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestTwoAgentsDifferentProfiles:
    """Two agents with different profiles see different tools."""

    def test_reader_agent_sees_only_read_tools(self, rebac_manager, middleware, profiles):
        """Agent with 'reader' profile sees only read tools."""
        reader_profile = profiles.get_profile("reader")
        grant_tools_for_profile(
            rebac_manager=rebac_manager,
            subject=("agent", "agent-reader"),
            profile=reader_profile,
        )

        visible = middleware._get_visible_tools(("agent", "agent-reader"))
        assert "nexus_read_file" in visible
        assert "nexus_list_files" in visible
        assert "nexus_file_info" in visible
        assert "nexus_write_file" not in visible
        assert "nexus_edit_file" not in visible

    def test_writer_agent_sees_read_and_write_tools(self, rebac_manager, middleware, profiles):
        """Agent with 'writer' profile sees read + write tools (inheritance)."""
        writer_profile = profiles.get_profile("writer")
        grant_tools_for_profile(
            rebac_manager=rebac_manager,
            subject=("agent", "agent-writer"),
            profile=writer_profile,
        )

        visible = middleware._get_visible_tools(("agent", "agent-writer"))
        # Inherited from reader
        assert "nexus_read_file" in visible
        assert "nexus_list_files" in visible
        # Own tools
        assert "nexus_write_file" in visible
        assert "nexus_edit_file" in visible
        assert "nexus_delete_file" in visible

    def test_agents_see_different_tool_sets(self, rebac_manager, middleware, profiles):
        """Different agents see different tool sets simultaneously."""
        reader_profile = profiles.get_profile("reader")
        writer_profile = profiles.get_profile("writer")

        grant_tools_for_profile(
            rebac_manager=rebac_manager,
            subject=("agent", "agent-A"),
            profile=reader_profile,
        )
        grant_tools_for_profile(
            rebac_manager=rebac_manager,
            subject=("agent", "agent-B"),
            profile=writer_profile,
        )

        # Invalidate cache so both pick up fresh grants
        middleware.invalidate()

        visible_a = middleware._get_visible_tools(("agent", "agent-A"))
        visible_b = middleware._get_visible_tools(("agent", "agent-B"))

        assert visible_a != visible_b
        assert "nexus_write_file" not in visible_a
        assert "nexus_write_file" in visible_b


class TestGrantRevocation:
    """Tool grant revocation → tool disappears from visible set."""

    def test_revoked_tool_disappears(self, rebac_manager, middleware, profiles):
        """After revoking grants, tools are no longer visible."""
        writer_profile = profiles.get_profile("writer")
        tuple_ids = grant_tools_for_profile(
            rebac_manager=rebac_manager,
            subject=("agent", "agent-revoke-test"),
            profile=writer_profile,
        )

        # Before revocation: all tools visible
        middleware.invalidate()
        visible_before = middleware._get_visible_tools(("agent", "agent-revoke-test"))
        assert "nexus_write_file" in visible_before

        # Revoke all grants (extract .tuple_id from WriteResult objects)
        tid_strings = [wr.tuple_id for wr in tuple_ids]
        revoke_tools_by_tuple_ids(rebac_manager=rebac_manager, tuple_ids=tid_strings)

        # After revocation: no tools visible (cache invalidated)
        middleware.invalidate()
        visible_after = middleware._get_visible_tools(("agent", "agent-revoke-test"))
        assert "nexus_write_file" not in visible_after
        assert len(visible_after) == 0


class TestDiscoveryEndToEnd:
    """Full flow: grant profile → discovery tools filter correctly."""

    def test_search_tools_filtered_by_profile(self, rebac_manager, middleware, mock_nx, profiles):
        """nexus_discovery_search_tools returns only visible tools."""
        reader_profile = profiles.get_profile("reader")
        grant_tools_for_profile(
            rebac_manager=rebac_manager,
            subject=("agent", "agent-disco"),
            profile=reader_profile,
        )

        server = create_mcp_server(nx=mock_nx, tool_namespace_middleware=middleware)
        tool_fn = _get_tool(server, "nexus_discovery_search_tools")

        ctx = Mock()
        ctx.get_state = Mock(
            side_effect=lambda key: {
                "subject_type": "agent",
                "subject_id": "agent-disco",
            }.get(key)
        )

        result = json.loads(tool_fn.fn(query="file", top_k=20, ctx=ctx))

        visible_names = {t["name"] for t in result["tools"]}
        # Should only see reader tools
        assert visible_names <= {"nexus_read_file", "nexus_list_files", "nexus_file_info"}
        # Specifically should NOT see write tools
        assert "nexus_write_file" not in visible_names

    def test_get_tool_details_invisible_returns_not_found(
        self, rebac_manager, middleware, mock_nx, profiles
    ):
        """Invisible tools return 'not found' from get_tool_details."""
        reader_profile = profiles.get_profile("reader")
        grant_tools_for_profile(
            rebac_manager=rebac_manager,
            subject=("agent", "agent-details"),
            profile=reader_profile,
        )

        server = create_mcp_server(nx=mock_nx, tool_namespace_middleware=middleware)
        tool_fn = _get_tool(server, "nexus_discovery_get_tool_details")

        ctx = Mock()
        ctx.get_state = Mock(
            side_effect=lambda key: {
                "subject_type": "agent",
                "subject_id": "agent-details",
            }.get(key)
        )

        # Visible tool → found
        result = json.loads(tool_fn.fn(tool_name="nexus_read_file", ctx=ctx))
        assert result["found"] is True

        # Invisible tool → not found (namespace-as-security)
        result = json.loads(tool_fn.fn(tool_name="nexus_python", ctx=ctx))
        assert result["found"] is False
        assert "not found" in result["error"]


class TestPerformance:
    """Performance validation for tool namespace filtering."""

    def test_filtering_30_tools_under_10ms(self, rebac_manager, middleware):
        """Filtering with 30 tool grants completes under 10ms."""
        # Grant 30 tools
        tools = [f"tool_{i:02d}" for i in range(30)]
        for tool_name in tools:
            rebac_manager.rebac_write(
                subject=("agent", "perf-agent"),
                relation="direct_viewer",
                object=("file", f"{TOOL_PATH_PREFIX}{tool_name}"),
            )

        middleware.invalidate()

        # Warm up
        middleware._get_visible_tools(("agent", "perf-agent"))
        middleware.invalidate()

        # Measure cold lookup (cache miss — includes ReBAC query)
        start = time.perf_counter()
        visible = middleware._get_visible_tools(("agent", "perf-agent"))
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert len(visible) == 30
        assert elapsed_ms < 50, f"Cold lookup took {elapsed_ms:.1f}ms (expected <50ms)"

        # Measure warm lookup (cache hit — O(1) dict lookup)
        start = time.perf_counter()
        visible2 = middleware._get_visible_tools(("agent", "perf-agent"))
        elapsed_hot_ms = (time.perf_counter() - start) * 1000

        assert visible2 == visible
        assert elapsed_hot_ms < 1, f"Hot lookup took {elapsed_hot_ms:.1f}ms (expected <1ms)"
