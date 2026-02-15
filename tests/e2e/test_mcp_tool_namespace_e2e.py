"""E2E test for MCP tool namespace — full grant → discover → call → revoke lifecycle.

Issue #1272: Validates the complete flow with a real ReBAC backend:
    1. Grant tools via profile → verify discovery filtering
    2. Verify invisible tools return "not found"
    3. Verify resolve_visible_tools() DRY path
    4. Verify revocation removes visibility
    5. Validate [TOOL-NS] and [PROFILES] log output
    6. Performance: cold + hot lookups under threshold

Uses real EnhancedReBACManager with in-memory SQLite (not mocks).

Usage:
    uv run pytest tests/e2e/test_mcp_tool_namespace_e2e.py -v --tb=short -p no:xdist -o "addopts=" --log-cli-level=INFO
"""

from __future__ import annotations

import json
import logging
import time
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from nexus.mcp.middleware import ToolNamespaceMiddleware
from nexus.mcp.profiles import (
    grant_tools_for_profile,
    load_profiles_from_dict,
    revoke_tools_by_tuple_ids,
)
from nexus.mcp.server import create_mcp_server
from nexus.services.permissions.rebac_manager_enhanced import EnhancedReBACManager
from nexus.storage.models import Base

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def rebac_engine():
    """In-memory SQLite engine with all ReBAC tables."""
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
        revision_window=1,
    )


@pytest.fixture
def mock_nx():
    """Minimal mock NexusFS for server creation."""
    nx = Mock()
    nx.read = Mock(return_value=b"hello world")
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
        return_value={
            "success": True,
            "diff": "",
            "applied_count": 0,
            "matches": [],
            "errors": [],
        }
    )
    return nx


@pytest.fixture
def profiles():
    """Test profile config: reader (3 tools) and writer (6 tools)."""
    return load_profiles_from_dict(
        {
            "profiles": {
                "reader": {
                    "description": "Read-only tools",
                    "tools": [
                        "nexus_read_file",
                        "nexus_list_files",
                        "nexus_file_info",
                    ],
                },
                "writer": {
                    "extends": "reader",
                    "description": "Read-write tools",
                    "tools": [
                        "nexus_write_file",
                        "nexus_edit_file",
                        "nexus_delete_file",
                    ],
                },
            },
            "default_profile": "reader",
        }
    )


def _make_ctx(subject_type: str, subject_id: str) -> Mock:
    """Create a mock FastMCP Context with subject state."""
    ctx = Mock()
    ctx.get_state = Mock(
        side_effect=lambda key: {
            "subject_type": subject_type,
            "subject_id": subject_id,
        }.get(key)
    )
    return ctx


def _get_tool(server, name):
    """Get a tool callable from the MCP server."""
    return server._tool_manager._tools[name]


# ---------------------------------------------------------------------------
# E2E Lifecycle Test
# ---------------------------------------------------------------------------


class TestToolNamespaceE2E:
    """Full lifecycle: grant → discover → call → revoke → verify gone."""

    def test_full_lifecycle(self, rebac_manager, middleware, mock_nx, profiles):
        """Complete grant → discover → use → revoke → verify lifecycle."""
        server = create_mcp_server(nx=mock_nx, tool_namespace_middleware=middleware)
        ctx = _make_ctx("agent", "lifecycle-agent")

        # --- Phase 1: No grants → no tools visible ---
        search_fn = _get_tool(server, "nexus_discovery_search_tools")
        result = json.loads(search_fn.fn(query="file", top_k=20, ctx=ctx))
        assert result["count"] == 0, "Agent with no grants should see zero tools"

        # --- Phase 2: Grant reader profile ---
        reader_profile = profiles.get_profile("reader")
        write_results = grant_tools_for_profile(
            rebac_manager=rebac_manager,
            subject=("agent", "lifecycle-agent"),
            profile=reader_profile,
        )
        assert len(write_results) == 3
        middleware.invalidate()

        # Discover: should see only reader tools (no invisible tools leak)
        result = json.loads(search_fn.fn(query="file read list", top_k=20, ctx=ctx))
        visible_names = {t["name"] for t in result["tools"]}
        allowed = {"nexus_read_file", "nexus_list_files", "nexus_file_info"}
        assert visible_names <= allowed, f"Invisible tools leaked: {visible_names - allowed}"
        assert len(visible_names) > 0, "At least one tool should match"
        assert "nexus_write_file" not in visible_names, "Write tool should be invisible"

        # Use: read_file should work
        read_fn = _get_tool(server, "nexus_read_file")
        read_result = read_fn.fn(path="/test.txt", ctx=ctx)
        assert "hello world" in read_result

        # Get details: visible tool returns details, invisible returns not found
        details_fn = _get_tool(server, "nexus_discovery_get_tool_details")
        visible_detail = json.loads(details_fn.fn(tool_name="nexus_read_file", ctx=ctx))
        assert visible_detail["found"] is True

        invisible_detail = json.loads(details_fn.fn(tool_name="nexus_delete_file", ctx=ctx))
        assert invisible_detail["found"] is False
        assert "not found" in invisible_detail["error"]

        # Load tools: visible loads, invisible goes to not_found
        load_fn = _get_tool(server, "nexus_discovery_load_tools")
        load_result = json.loads(
            load_fn.fn(
                tool_names=["nexus_read_file", "nexus_delete_file"],
                ctx=ctx,
            )
        )
        assert "nexus_read_file" in load_result["loaded"]
        assert "nexus_delete_file" in load_result["not_found"]

        # List servers: tool counts reflect filtering
        list_fn = _get_tool(server, "nexus_discovery_list_servers")
        list_result = json.loads(list_fn.fn(ctx=ctx))
        assert list_result["total_tools"] <= 3, "Reader sees at most 3 tools"

        # --- Phase 3: Revoke all grants ---
        tid_strings = [wr.tuple_id for wr in write_results]
        deleted = revoke_tools_by_tuple_ids(
            rebac_manager=rebac_manager,
            tuple_ids=tid_strings,
        )
        assert deleted == 3
        middleware.invalidate()

        # --- Phase 4: Verify tools are gone ---
        result = json.loads(search_fn.fn(query="file", top_k=20, ctx=ctx))
        assert result["count"] == 0, "After revocation, no tools should be visible"

        logger.info("PASS: Full lifecycle test completed successfully")


# ---------------------------------------------------------------------------
# resolve_visible_tools() DRY path
# ---------------------------------------------------------------------------


class TestResolveVisibleToolsDRY:
    """Verify the DRY path: middleware.resolve_visible_tools() → server._get_visible_tool_names()."""

    def test_middleware_and_server_agree(self, rebac_manager, middleware, profiles):
        """resolve_visible_tools() returns same set as _get_visible_tools()."""
        reader_profile = profiles.get_profile("reader")
        grant_tools_for_profile(
            rebac_manager=rebac_manager,
            subject=("agent", "dry-agent"),
            profile=reader_profile,
        )
        middleware.invalidate()

        ctx = _make_ctx("agent", "dry-agent")

        # Direct path (middleware internal)
        direct = middleware._get_visible_tools(("agent", "dry-agent"))
        # DRY path (middleware public API, used by discovery tools)
        via_public = middleware.resolve_visible_tools(ctx)

        assert direct == via_public
        assert "nexus_read_file" in direct
        assert "nexus_write_file" not in direct

    def test_resolve_none_ctx_returns_none(self, middleware):
        """None ctx → None (no filtering)."""
        assert middleware.resolve_visible_tools(None) is None

    def test_resolve_no_subject_returns_none(self, middleware):
        """Ctx without subject → None (backward compat)."""
        ctx = Mock()
        ctx.get_state = Mock(return_value=None)
        assert middleware.resolve_visible_tools(ctx) is None


# ---------------------------------------------------------------------------
# Log validation
# ---------------------------------------------------------------------------


class TestLogOutput:
    """Verify [TOOL-NS] and [PROFILES] log messages are emitted."""

    def test_grant_emits_profiles_log(self, rebac_manager, profiles, caplog):
        """grant_tools_for_profile() logs [PROFILES] message."""
        reader_profile = profiles.get_profile("reader")

        with caplog.at_level(logging.INFO, logger="nexus.mcp.profiles"):
            grant_tools_for_profile(
                rebac_manager=rebac_manager,
                subject=("agent", "log-agent"),
                profile=reader_profile,
            )

        assert "[PROFILES]" in caplog.text
        assert "Granted 3 tools" in caplog.text
        assert "log-agent" in caplog.text
        logger.info("PASS: Grant log validation")

    def test_revoke_emits_profiles_log(self, rebac_manager, profiles, caplog):
        """revoke_tools_by_tuple_ids() logs [PROFILES] message."""
        reader_profile = profiles.get_profile("reader")
        results = grant_tools_for_profile(
            rebac_manager=rebac_manager,
            subject=("agent", "revoke-log-agent"),
            profile=reader_profile,
        )

        tid_strings = [wr.tuple_id for wr in results]

        with caplog.at_level(logging.INFO, logger="nexus.mcp.profiles"):
            revoke_tools_by_tuple_ids(
                rebac_manager=rebac_manager,
                tuple_ids=tid_strings,
            )

        assert "[PROFILES]" in caplog.text
        assert "Revoked" in caplog.text
        logger.info("PASS: Revoke log validation")

    def test_tool_rebuild_emits_debug_log(self, rebac_manager, middleware, profiles, caplog):
        """_rebuild_tool_set() emits [TOOL-NS] debug log."""
        reader_profile = profiles.get_profile("reader")
        grant_tools_for_profile(
            rebac_manager=rebac_manager,
            subject=("agent", "rebuild-log-agent"),
            profile=reader_profile,
        )
        middleware.invalidate()

        with caplog.at_level(logging.DEBUG, logger="nexus.mcp.middleware"):
            middleware._get_visible_tools(("agent", "rebuild-log-agent"))

        assert "[TOOL-NS]" in caplog.text
        assert "Rebuilt tool set" in caplog.text
        logger.info("PASS: Rebuild log validation")


# ---------------------------------------------------------------------------
# Performance validation
# ---------------------------------------------------------------------------


class TestPerformanceE2E:
    """E2E performance benchmarks for tool namespace operations."""

    def test_grant_performance(self, rebac_manager, profiles):
        """Granting a 6-tool profile completes under 100ms."""
        writer_profile = profiles.get_profile("writer")

        start = time.perf_counter()
        results = grant_tools_for_profile(
            rebac_manager=rebac_manager,
            subject=("agent", "perf-grant-agent"),
            profile=writer_profile,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert len(results) == 6
        assert elapsed_ms < 100, f"Grant took {elapsed_ms:.1f}ms (expected <100ms)"
        logger.info("Grant performance: %.1fms for %d tools", elapsed_ms, len(results))

    def test_cold_lookup_performance(self, rebac_manager, middleware, profiles):
        """Cold lookup (cache miss + ReBAC query) under 50ms."""
        writer_profile = profiles.get_profile("writer")
        grant_tools_for_profile(
            rebac_manager=rebac_manager,
            subject=("agent", "perf-cold-agent"),
            profile=writer_profile,
        )
        middleware.invalidate()

        start = time.perf_counter()
        visible = middleware._get_visible_tools(("agent", "perf-cold-agent"))
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert len(visible) == 6
        assert elapsed_ms < 50, f"Cold lookup took {elapsed_ms:.1f}ms (expected <50ms)"
        logger.info("Cold lookup: %.1fms for %d tools", elapsed_ms, len(visible))

    def test_hot_lookup_performance(self, rebac_manager, middleware, profiles):
        """Hot lookup (cache hit) under 1ms."""
        writer_profile = profiles.get_profile("writer")
        grant_tools_for_profile(
            rebac_manager=rebac_manager,
            subject=("agent", "perf-hot-agent"),
            profile=writer_profile,
        )
        middleware.invalidate()

        # Prime the cache
        middleware._get_visible_tools(("agent", "perf-hot-agent"))

        start = time.perf_counter()
        visible = middleware._get_visible_tools(("agent", "perf-hot-agent"))
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert len(visible) == 6
        assert elapsed_ms < 1, f"Hot lookup took {elapsed_ms:.3f}ms (expected <1ms)"
        logger.info("Hot lookup: %.3fms (cache hit)", elapsed_ms)

    def test_discovery_search_performance(self, rebac_manager, middleware, mock_nx, profiles):
        """Discovery search with namespace filtering under 50ms."""
        reader_profile = profiles.get_profile("reader")
        grant_tools_for_profile(
            rebac_manager=rebac_manager,
            subject=("agent", "perf-disco-agent"),
            profile=reader_profile,
        )
        middleware.invalidate()

        server = create_mcp_server(nx=mock_nx, tool_namespace_middleware=middleware)
        search_fn = _get_tool(server, "nexus_discovery_search_tools")
        ctx = _make_ctx("agent", "perf-disco-agent")

        # Warm up
        search_fn.fn(query="file", top_k=5, ctx=ctx)
        middleware.invalidate()

        # Cold search (includes ReBAC + BM25 + filtering)
        start = time.perf_counter()
        result = json.loads(search_fn.fn(query="read file", top_k=5, ctx=ctx))
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert result["count"] > 0
        assert elapsed_ms < 50, f"Discovery search took {elapsed_ms:.1f}ms (expected <50ms)"
        logger.info("Discovery search: %.1fms, found %d tools", elapsed_ms, result["count"])


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    """No middleware → all tools visible (backward compat)."""

    def test_no_middleware_returns_all_tools(self, mock_nx):
        """Server without middleware returns unfiltered tool results."""
        server = create_mcp_server(nx=mock_nx)
        search_fn = _get_tool(server, "nexus_discovery_search_tools")

        result = json.loads(search_fn.fn(query="file", top_k=20, ctx=None))
        assert result["count"] > 3, "Without middleware, all tools should be visible"
        logger.info("PASS: Backward compat — %d tools visible", result["count"])

    def test_no_ctx_returns_all_tools(self, mock_nx, middleware):
        """With middleware but no ctx, all tools visible."""
        server = create_mcp_server(nx=mock_nx, tool_namespace_middleware=middleware)
        search_fn = _get_tool(server, "nexus_discovery_search_tools")

        result = json.loads(search_fn.fn(query="file", top_k=20, ctx=None))
        assert result["count"] > 3, "No ctx → no filtering"
        logger.info("PASS: No ctx — %d tools visible", result["count"])
