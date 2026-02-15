"""Tests for MCP discovery tools — namespace filtering (Issue #1272, Phase 4).

Verifies that all four discovery tools filter results through
the ToolNamespaceMiddleware's visible tool set:
    - nexus_discovery_search_tools: over-fetch + post-filter
    - nexus_discovery_list_servers: filtered tool counts
    - nexus_discovery_get_tool_details: invisible → "not found"
    - nexus_discovery_load_tools: invisible → "not found"
"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from nexus.mcp.server import create_mcp_server

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_tool(server, tool_name: str):
    """Helper to get a tool callable from the MCP server."""
    return server._tool_manager._tools[tool_name]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_nx():
    """Minimal mock NexusFS for server creation."""
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
def mock_middleware():
    """Mock ToolNamespaceMiddleware with configurable visible tools."""
    visible = frozenset({"nexus_read_file", "nexus_write_file"})

    def _resolve(ctx):
        """Return None for no-ctx/no-subject, frozenset for subjects."""
        if ctx is None:
            return None
        if not hasattr(ctx, "get_state"):
            return None
        try:
            st = ctx.get_state("subject_type")
            si = ctx.get_state("subject_id")
            if st and si:
                return visible
        except Exception:
            pass
        return None

    mw = Mock()
    mw.resolve_visible_tools = Mock(side_effect=_resolve)
    mw._get_visible_tools = Mock(return_value=visible)
    return mw


@pytest.fixture
def mock_ctx():
    """Mock FastMCP Context with subject state."""
    ctx = Mock()
    ctx.get_state = Mock(
        side_effect=lambda key: {
            "subject_type": "agent",
            "subject_id": "agent-1",
        }.get(key)
    )
    return ctx


@pytest.fixture
def server_with_namespace(mock_nx, mock_middleware):
    """MCP server with namespace middleware wired in."""
    return create_mcp_server(nx=mock_nx, tool_namespace_middleware=mock_middleware)


@pytest.fixture
def server_without_namespace(mock_nx):
    """MCP server without namespace middleware (backward compat)."""
    return create_mcp_server(nx=mock_nx)


# ---------------------------------------------------------------------------
# nexus_discovery_search_tools
# ---------------------------------------------------------------------------


class TestSearchToolsNamespace:
    def test_filters_results_through_visible_set(self, server_with_namespace, mock_ctx):
        """Search results are filtered to only include visible tools."""
        tool_fn = get_tool(server_with_namespace, "nexus_discovery_search_tools")
        result = json.loads(tool_fn.fn(query="read file", top_k=5, ctx=mock_ctx))

        # All returned tools should be in the visible set
        for tool in result["tools"]:
            assert tool["name"] in {"nexus_read_file", "nexus_write_file"}, (
                f"Invisible tool '{tool['name']}' leaked through namespace filter"
            )

    def test_no_namespace_returns_all(self, server_without_namespace):
        """Without middleware, all tools are returned."""
        tool_fn = get_tool(server_without_namespace, "nexus_discovery_search_tools")
        result = json.loads(tool_fn.fn(query="file", top_k=20, ctx=None))

        # Should have many tools (all bootstrapped Nexus tools)
        assert result["count"] > 2

    def test_over_fetches_to_compensate_for_filtering(self, server_with_namespace, mock_ctx):
        """When namespace is active, search over-fetches to get enough results."""
        tool_fn = get_tool(server_with_namespace, "nexus_discovery_search_tools")
        # Request top_k=1, but the over-fetch should still find visible tools
        result = json.loads(tool_fn.fn(query="read", top_k=1, ctx=mock_ctx))

        assert result["count"] <= 1
        for tool in result["tools"]:
            assert tool["name"] in {"nexus_read_file", "nexus_write_file"}


# ---------------------------------------------------------------------------
# nexus_discovery_list_servers
# ---------------------------------------------------------------------------


class TestListServersNamespace:
    def test_tool_counts_filtered_by_namespace(self, server_with_namespace, mock_ctx):
        """Tool counts reflect only visible tools, not all indexed tools."""
        tool_fn = get_tool(server_with_namespace, "nexus_discovery_list_servers")
        result = json.loads(tool_fn.fn(ctx=mock_ctx))

        # total_tools should equal sum of per-server visible counts
        assert result["total_tools"] == sum(result["server_tool_counts"].values())
        # With only 2 visible tools, total should be ≤ 2
        assert result["total_tools"] <= 2

    def test_no_namespace_shows_all_tools(self, server_without_namespace):
        """Without middleware, full tool counts are returned."""
        tool_fn = get_tool(server_without_namespace, "nexus_discovery_list_servers")
        result = json.loads(tool_fn.fn(ctx=None))

        # Should have many tools
        assert result["total_tools"] > 2

    def test_servers_still_listed_even_with_zero_visible(self, mock_nx):
        """Servers appear even if all their tools are filtered out."""
        mw = Mock()
        mw.resolve_visible_tools = Mock(return_value=frozenset())  # No tools visible

        server = create_mcp_server(nx=mock_nx, tool_namespace_middleware=mw)
        tool_fn = get_tool(server, "nexus_discovery_list_servers")

        ctx = Mock()
        ctx.get_state = Mock(
            side_effect=lambda key: {
                "subject_type": "agent",
                "subject_id": "agent-2",
            }.get(key)
        )

        result = json.loads(tool_fn.fn(ctx=ctx))

        assert "nexus" in result["servers"]
        assert result["total_tools"] == 0


# ---------------------------------------------------------------------------
# nexus_discovery_get_tool_details
# ---------------------------------------------------------------------------


class TestGetToolDetailsNamespace:
    def test_visible_tool_returns_details(self, server_with_namespace, mock_ctx):
        """Visible tools return full details."""
        tool_fn = get_tool(server_with_namespace, "nexus_discovery_get_tool_details")
        result = json.loads(tool_fn.fn(tool_name="nexus_read_file", ctx=mock_ctx))

        assert result["found"] is True
        assert result["name"] == "nexus_read_file"

    def test_invisible_tool_returns_not_found(self, server_with_namespace, mock_ctx):
        """Invisible tools return 'not found' — namespace-as-security."""
        tool_fn = get_tool(server_with_namespace, "nexus_discovery_get_tool_details")
        result = json.loads(tool_fn.fn(tool_name="nexus_delete_file", ctx=mock_ctx))

        assert result["found"] is False
        assert "not found" in result["error"]

    def test_no_namespace_returns_any_tool(self, server_without_namespace):
        """Without middleware, any indexed tool returns details."""
        tool_fn = get_tool(server_without_namespace, "nexus_discovery_get_tool_details")
        result = json.loads(tool_fn.fn(tool_name="nexus_delete_file", ctx=None))

        assert result["found"] is True

    def test_truly_nonexistent_tool_no_namespace(self, server_without_namespace):
        """Tool not in index (no namespace active) still returns 'not found'."""
        tool_fn = get_tool(server_without_namespace, "nexus_discovery_get_tool_details")
        result = json.loads(tool_fn.fn(tool_name="nonexistent_tool_xyz", ctx=None))

        assert result["found"] is False
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# nexus_discovery_load_tools
# ---------------------------------------------------------------------------


class TestLoadToolsNamespace:
    def test_invisible_tool_reported_as_not_found(self, server_with_namespace, mock_ctx):
        """Invisible tools go to not_found list, not loaded."""
        tool_fn = get_tool(server_with_namespace, "nexus_discovery_load_tools")
        result = json.loads(
            tool_fn.fn(
                tool_names=["nexus_read_file", "nexus_delete_file"],
                ctx=mock_ctx,
            )
        )

        assert "nexus_read_file" in result["loaded"]
        assert "nexus_delete_file" in result["not_found"]

    def test_no_namespace_loads_any_tool(self, server_without_namespace):
        """Without middleware, any indexed tool can be loaded."""
        tool_fn = get_tool(server_without_namespace, "nexus_discovery_load_tools")
        result = json.loads(
            tool_fn.fn(
                tool_names=["nexus_delete_file", "nexus_glob"],
                ctx=None,
            )
        )

        assert "nexus_delete_file" in result["loaded"]
        assert "nexus_glob" in result["loaded"]
        assert result["not_found"] == []

    def test_all_invisible_none_loaded(self, mock_nx):
        """When all requested tools are invisible, none are loaded."""
        mw = Mock()
        mw.resolve_visible_tools = Mock(return_value=frozenset())

        server = create_mcp_server(nx=mock_nx, tool_namespace_middleware=mw)
        tool_fn = get_tool(server, "nexus_discovery_load_tools")

        ctx = Mock()
        ctx.get_state = Mock(
            side_effect=lambda key: {
                "subject_type": "agent",
                "subject_id": "agent-3",
            }.get(key)
        )

        result = json.loads(
            tool_fn.fn(
                tool_names=["nexus_read_file", "nexus_write_file"],
                ctx=ctx,
            )
        )

        assert result["loaded"] == []
        assert set(result["not_found"]) == {"nexus_read_file", "nexus_write_file"}


# ---------------------------------------------------------------------------
# _get_visible_tool_names — edge cases
# ---------------------------------------------------------------------------


class TestGetVisibleToolNames:
    def test_no_ctx_returns_none_no_filtering(self, server_with_namespace):
        """When ctx is None, no filtering happens (backward compat)."""
        tool_fn = get_tool(server_with_namespace, "nexus_discovery_search_tools")
        # Call with ctx=None — should return unfiltered results
        result = json.loads(tool_fn.fn(query="file", top_k=20, ctx=None))
        assert result["count"] > 2

    def test_ctx_without_subject_returns_all(self, server_with_namespace):
        """When ctx exists but has no subject state, no filtering happens."""
        ctx = Mock()
        ctx.get_state = Mock(return_value=None)

        tool_fn = get_tool(server_with_namespace, "nexus_discovery_search_tools")
        result = json.loads(tool_fn.fn(query="file", top_k=20, ctx=ctx))
        assert result["count"] > 2
