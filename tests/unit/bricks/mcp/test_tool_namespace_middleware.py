"""Tests for ToolNamespaceMiddleware (Issue #1272).

Tests cover:
- tools/list filtering by subject grants
- tools/call rejection for invisible tools
- Cache behavior (hits, misses, invalidation)
- Subject extraction from context
- Edge cases (no subject, disabled middleware, empty grants)
"""

import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from nexus.bricks.mcp.middleware import ToolNamespaceMiddleware
from nexus.bricks.mcp.profiles import (
    grant_tools_for_profile,
    load_profiles,
    load_profiles_from_dict,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeTool:
    """Minimal tool object with a name attribute (mirrors fastmcp.tools.tool.Tool)."""

    name: str


class FakeContext:
    """Fake FastMCP context with get_state/set_state support."""

    def __init__(self, state: dict[str, Any] | None = None):
        self._state = state or {}

    def get_state(self, key: str) -> Any:
        if key not in self._state:
            raise KeyError(key)
        return self._state[key]

    def set_state(self, key: str, value: Any) -> None:
        self._state[key] = value


class AsyncFakeContext:
    """FastMCP 3.x-style context where get_state/set_state are awaitable."""

    def __init__(self, state: dict[str, Any] | None = None):
        self._state = state or {}

    async def get_state(self, key: str) -> Any:
        if key not in self._state:
            raise KeyError(key)
        return self._state[key]

    async def set_state(self, key: str, value: Any) -> None:
        self._state[key] = value


@dataclass
class FakeMiddlewareContext:
    """Fake MiddlewareContext matching the FastMCP interface."""

    message: Any = None
    fastmcp_context: Any = None
    method: str | None = None


@dataclass
class FakeCallToolParams:
    """Fake CallToolRequestParams."""

    name: str
    arguments: dict[str, Any] | None = None


def make_rebac_mock(
    granted_objects: list[tuple[str, str]] | None = None,
    zone_revision: int = 0,
) -> MagicMock:
    """Create a mock ReBAC manager with configurable grants."""
    rebac = MagicMock()
    rebac.rebac_list_objects.return_value = granted_objects or []
    rebac.get_zone_revision.return_value = zone_revision
    return rebac


def make_middleware(
    granted_tools: list[str] | None = None,
    zone_revision: int = 0,
    enabled: bool = True,
    zone_id: str | None = None,
) -> ToolNamespaceMiddleware:
    """Create middleware with pre-configured tool grants."""
    objects = [("file", f"/tools/{t}") for t in (granted_tools or [])]
    rebac = make_rebac_mock(granted_objects=objects, zone_revision=zone_revision)
    return ToolNamespaceMiddleware(
        rebac_manager=rebac,
        zone_id=zone_id,
        enabled=enabled,
    )


def make_subject_asserting_middleware(expected_subject: tuple[str, str]) -> ToolNamespaceMiddleware:
    """Middleware whose ReBAC double proves the extracted subject value."""
    rebac = MagicMock()
    rebac.get_zone_revision.return_value = 0

    def list_objects(
        *,
        subject: tuple[str, str],
        permission: str,
        object_type: str,
        zone_id: str | None = None,
        limit: int,
    ) -> list[tuple[str, str]]:
        assert subject == expected_subject
        assert permission == "read"
        assert object_type == "file"
        assert zone_id is None
        assert limit == 10_000
        return [("file", "/tools/nexus_read_file")]

    rebac.rebac_list_objects.side_effect = list_objects
    return ToolNamespaceMiddleware(rebac_manager=rebac)


class MemoryToolGrantReBAC:
    """Small ReBAC test double for profile grant -> namespace filter integration."""

    def __init__(self) -> None:
        self._objects_by_subject: dict[
            tuple[tuple[str, str], str | None], set[tuple[str, str]]
        ] = {}
        self._revision = 0

    def rebac_write(
        self,
        *,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> Any:
        assert relation == "direct_viewer"
        self._objects_by_subject.setdefault((subject, zone_id), set()).add(object)
        self._revision += 1
        return SimpleNamespace(tuple_id=f"tuple-{self._revision}", revision=self._revision)

    def rebac_list_objects(
        self,
        *,
        subject: tuple[str, str],
        permission: str,
        object_type: str,
        zone_id: str | None = None,
        limit: int,
    ) -> list[tuple[str, str]]:
        assert permission == "read"
        objects = self._objects_by_subject.get((subject, zone_id), set())
        return [obj for obj in sorted(objects) if obj[0] == object_type][:limit]

    def get_zone_revision(self, zone_id: str | None) -> int:  # noqa: ARG002
        return self._revision


# ---------------------------------------------------------------------------
# tools/list filtering
# ---------------------------------------------------------------------------


class TestOnListTools:
    @pytest.mark.asyncio
    async def test_returns_only_visible_tools(self):
        mw = make_middleware(granted_tools=["nexus_read_file", "nexus_list_files"])

        all_tools = [
            FakeTool(name="nexus_read_file"),
            FakeTool(name="nexus_write_file"),
            FakeTool(name="nexus_list_files"),
        ]

        ctx = FakeMiddlewareContext(
            fastmcp_context=FakeContext({"subject_type": "agent", "subject_id": "A"}),
            method="tools/list",
        )

        async def call_next(context: Any) -> Sequence[Any]:
            return all_tools

        result = await mw.on_list_tools(ctx, call_next)  # type: ignore[arg-type]
        names = [t.name for t in result]
        assert names == ["nexus_read_file", "nexus_list_files"]

    @pytest.mark.asyncio
    async def test_async_fastmcp_context_state_filters_tools(self):
        """FastMCP 3.x exposes Context.get_state as async."""
        mw = make_subject_asserting_middleware(("agent", "A"))

        all_tools = [
            FakeTool(name="nexus_read_file"),
            FakeTool(name="nexus_write_file"),
        ]
        ctx = FakeMiddlewareContext(
            fastmcp_context=AsyncFakeContext({"subject_type": "agent", "subject_id": "A"}),
            method="tools/list",
        )

        async def call_next(context: Any) -> Sequence[Any]:
            return all_tools

        result = await mw.on_list_tools(ctx, call_next)  # type: ignore[arg-type]
        assert [t.name for t in result] == ["nexus_read_file"]

    @pytest.mark.asyncio
    async def test_no_grants_returns_empty(self):
        mw = make_middleware(granted_tools=[])

        all_tools = [FakeTool(name="nexus_read_file")]
        ctx = FakeMiddlewareContext(
            fastmcp_context=FakeContext({"subject_type": "agent", "subject_id": "A"}),
        )

        async def call_next(context: Any) -> Sequence[Any]:
            return all_tools

        result = await mw.on_list_tools(ctx, call_next)  # type: ignore[arg-type]
        assert result == []

    @pytest.mark.asyncio
    async def test_all_grants_returns_full_list(self):
        tools = ["nexus_read_file", "nexus_write_file"]
        mw = make_middleware(granted_tools=tools)

        all_tools = [FakeTool(name=t) for t in tools]
        ctx = FakeMiddlewareContext(
            fastmcp_context=FakeContext({"subject_type": "agent", "subject_id": "A"}),
        )

        async def call_next(context: Any) -> Sequence[Any]:
            return all_tools

        result = await mw.on_list_tools(ctx, call_next)  # type: ignore[arg-type]
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_no_subject_returns_all_tools(self):
        """No subject identity → backward compat / admin → all tools visible."""
        mw = make_middleware(granted_tools=["nexus_read_file"])

        all_tools = [FakeTool(name="nexus_read_file"), FakeTool(name="nexus_write_file")]
        ctx = FakeMiddlewareContext(fastmcp_context=None)

        async def call_next(context: Any) -> Sequence[Any]:
            return all_tools

        result = await mw.on_list_tools(ctx, call_next)  # type: ignore[arg-type]
        assert len(result) == 2  # All tools returned, no filtering

    @pytest.mark.asyncio
    async def test_disabled_middleware_returns_all(self):
        mw = make_middleware(granted_tools=["nexus_read_file"], enabled=False)

        all_tools = [FakeTool(name="nexus_read_file"), FakeTool(name="nexus_write_file")]
        ctx = FakeMiddlewareContext(
            fastmcp_context=FakeContext({"subject_type": "agent", "subject_id": "A"}),
        )

        async def call_next(context: Any) -> Sequence[Any]:
            return all_tools

        result = await mw.on_list_tools(ctx, call_next)  # type: ignore[arg-type]
        assert len(result) == 2  # All tools, middleware disabled


# ---------------------------------------------------------------------------
# tools/call validation
# ---------------------------------------------------------------------------


class TestOnCallTool:
    @pytest.mark.asyncio
    async def test_allowed_tool_passes_through(self):
        mw = make_middleware(granted_tools=["nexus_read_file"])

        ctx = FakeMiddlewareContext(
            message=FakeCallToolParams(name="nexus_read_file"),
            fastmcp_context=FakeContext({"subject_type": "agent", "subject_id": "A"}),
        )

        expected_result = MagicMock()

        async def call_next(context: Any) -> Any:
            return expected_result

        result = await mw.on_call_tool(ctx, call_next)  # type: ignore[arg-type]
        assert result is expected_result

    @pytest.mark.asyncio
    async def test_async_fastmcp_context_state_allows_visible_call(self):
        """FastMCP 3.x async context state must not hide granted tools."""
        mw = make_subject_asserting_middleware(("agent", "A"))

        ctx = FakeMiddlewareContext(
            message=FakeCallToolParams(name="nexus_read_file"),
            fastmcp_context=AsyncFakeContext({"subject_type": "agent", "subject_id": "A"}),
        )

        expected_result = MagicMock()

        async def call_next(context: Any) -> Any:
            return expected_result

        result = await mw.on_call_tool(ctx, call_next)  # type: ignore[arg-type]
        assert result is expected_result

    @pytest.mark.asyncio
    async def test_invisible_tool_returns_not_found(self):
        mw = make_middleware(granted_tools=["nexus_read_file"])

        ctx = FakeMiddlewareContext(
            message=FakeCallToolParams(name="nexus_python"),
            fastmcp_context=FakeContext({"subject_type": "agent", "subject_id": "A"}),
        )

        async def call_next(context: Any) -> Any:
            pytest.fail("call_next should not be called for invisible tool")

        result = await mw.on_call_tool(ctx, call_next)  # type: ignore[arg-type]
        # Result should be a ToolResult with "not found" error
        assert result is not None
        # Check the error message content
        assert hasattr(result, "content")

    @pytest.mark.asyncio
    async def test_invisible_tool_error_says_not_found_not_permission_denied(self):
        """Namespace-as-security: invisible tools must return 'not found', not 'denied'."""
        mw = make_middleware(granted_tools=[])

        ctx = FakeMiddlewareContext(
            message=FakeCallToolParams(name="nexus_python"),
            fastmcp_context=FakeContext({"subject_type": "agent", "subject_id": "A"}),
        )

        async def call_next(context: Any) -> Any:
            pytest.fail("Should not reach here")

        result = await mw.on_call_tool(ctx, call_next)  # type: ignore[arg-type]
        # Verify the error message uses "not found"
        content_text = result.content[0].text
        assert "not found" in content_text.lower()
        assert "permission" not in content_text.lower()
        assert "denied" not in content_text.lower()

    @pytest.mark.asyncio
    async def test_no_subject_allows_call(self):
        """No subject → backward compat → allow all calls."""
        mw = make_middleware(granted_tools=[])

        ctx = FakeMiddlewareContext(
            message=FakeCallToolParams(name="nexus_python"),
            fastmcp_context=None,
        )

        expected_result = MagicMock()

        async def call_next(context: Any) -> Any:
            return expected_result

        result = await mw.on_call_tool(ctx, call_next)  # type: ignore[arg-type]
        assert result is expected_result


class TestProfileGrantIntegration:
    @pytest.mark.asyncio
    async def test_profile_grants_drive_list_filtering_and_call_denial(self):
        config = load_profiles_from_dict(
            {
                "profiles": {
                    "minimal": {"tools": ["nexus_read_file"]},
                    "coding": {
                        "extends": "minimal",
                        "tools": ["nexus_write_file"],
                    },
                }
            }
        )
        profile = config.get_profile("coding")
        assert profile is not None

        rebac = MemoryToolGrantReBAC()
        rebac_manager = cast(Any, rebac)
        grant_tools_for_profile(
            rebac_manager=rebac_manager,
            subject=("agent", "alice"),
            profile=profile,
            zone_id="sandbox-agent-1",
        )

        mw = ToolNamespaceMiddleware(rebac_manager=rebac_manager, zone_id="sandbox-agent-1")
        ctx = FakeMiddlewareContext(
            fastmcp_context=FakeContext({"subject_type": "agent", "subject_id": "alice"}),
        )
        all_tools = [
            FakeTool(name="nexus_read_file"),
            FakeTool(name="nexus_write_file"),
            FakeTool(name="nexus_python"),
        ]

        async def list_next(context: Any) -> Sequence[Any]:
            return all_tools

        listed = await mw.on_list_tools(cast(Any, ctx), cast(Any, list_next))
        assert [tool.name for tool in listed] == ["nexus_read_file", "nexus_write_file"]

        allowed_result = MagicMock()

        async def call_next(context: Any) -> Any:
            return allowed_result

        allowed_ctx = FakeMiddlewareContext(
            message=FakeCallToolParams(name="nexus_write_file"),
            fastmcp_context=ctx.fastmcp_context,
        )
        assert await mw.on_call_tool(cast(Any, allowed_ctx), cast(Any, call_next)) is allowed_result

        denied_ctx = FakeMiddlewareContext(
            message=FakeCallToolParams(name="nexus_python"),
            fastmcp_context=ctx.fastmcp_context,
        )
        denied = await mw.on_call_tool(cast(Any, denied_ctx), cast(Any, call_next))
        denied_text = denied.content[0].text
        assert "not found" in denied_text.lower()
        assert "permission" not in denied_text.lower()


class TestDefaultToolProfileEnforcement:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("profile_name", "visible", "hidden"),
        [
            (
                "minimal",
                ["nexus_read_file", "nexus_list_files", "nexus_file_info", "nexus_glob"],
                "nexus_write_file",
            ),
            (
                "coding",
                ["nexus_read_file", "nexus_write_file", "nexus_edit_file", "nexus_grep"],
                "nexus_python",
            ),
            (
                "search",
                ["nexus_read_file", "nexus_grep", "nexus_semantic_search"],
                "nexus_write_file",
            ),
            (
                "execution",
                ["nexus_write_file", "nexus_python", "nexus_bash", "nexus_sandbox_create"],
                "nexus_discovery_search_tools",
            ),
            (
                "full",
                [
                    "nexus_python",
                    "nexus_discovery_search_tools",
                    "nexus_list_workflows",
                    "nexus_hub_admin",
                ],
                "nexus_context_branch",
            ),
        ],
    )
    async def test_default_profile_grants_filter_list_and_call(
        self,
        profile_name: str,
        visible: list[str],
        hidden: str,
    ) -> None:
        config_path = Path(__file__).parents[4] / "src" / "nexus" / "config" / "tool_profiles.yaml"
        config = load_profiles(config_path)
        profile = config.get_profile(profile_name)
        assert profile is not None

        rebac = MemoryToolGrantReBAC()
        rebac_manager = cast(Any, rebac)
        grant_tools_for_profile(
            rebac_manager=rebac_manager,
            subject=("agent", profile_name),
            profile=profile,
            zone_id="sandbox-agent-4131",
        )

        mw = ToolNamespaceMiddleware(rebac_manager=rebac_manager, zone_id="sandbox-agent-4131")
        ctx = FakeMiddlewareContext(
            fastmcp_context=FakeContext({"subject_type": "agent", "subject_id": profile_name}),
        )
        all_tools = [FakeTool(name=name) for name in [*visible, hidden]]

        async def list_next(context: Any) -> Sequence[Any]:
            return all_tools

        listed = await mw.on_list_tools(cast(Any, ctx), cast(Any, list_next))
        assert {tool.name for tool in listed} == set(visible)

        allowed_result = MagicMock(name=f"{profile_name}-allowed")

        async def call_next(context: Any) -> Any:
            return allowed_result

        allowed_ctx = FakeMiddlewareContext(
            message=FakeCallToolParams(name=visible[0]),
            fastmcp_context=ctx.fastmcp_context,
        )
        assert await mw.on_call_tool(cast(Any, allowed_ctx), cast(Any, call_next)) is allowed_result

        hidden_ctx = FakeMiddlewareContext(
            message=FakeCallToolParams(name=hidden),
            fastmcp_context=ctx.fastmcp_context,
        )
        denied = await mw.on_call_tool(cast(Any, hidden_ctx), cast(Any, call_next))
        denied_text = denied.content[0].text.lower()
        assert "not found" in denied_text
        assert "permission" not in denied_text


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------


class TestCacheBehavior:
    @pytest.mark.asyncio
    async def test_cache_hit_on_second_call(self):
        mw = make_middleware(granted_tools=["nexus_read_file"])

        ctx = FakeMiddlewareContext(
            fastmcp_context=FakeContext({"subject_type": "agent", "subject_id": "A"}),
        )

        async def call_next(context: Any) -> Sequence[Any]:
            return [FakeTool(name="nexus_read_file")]

        # First call: cache miss
        await mw.on_list_tools(ctx, call_next)  # type: ignore[arg-type]
        assert mw._cache_misses == 1
        assert mw._cache_hits == 0

        # Second call: cache hit
        await mw.on_list_tools(ctx, call_next)  # type: ignore[arg-type]
        assert mw._cache_hits == 1

        # rebac_list_objects should have been called only once
        mw._rebac_manager.rebac_list_objects.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_invalidation_on_revision_change(self):
        objects = [("file", "/tools/nexus_read_file")]
        rebac = make_rebac_mock(granted_objects=objects, zone_revision=0)
        mw = ToolNamespaceMiddleware(rebac_manager=rebac, revision_window=10)

        ctx = FakeMiddlewareContext(
            fastmcp_context=FakeContext({"subject_type": "agent", "subject_id": "A"}),
        )

        async def call_next(context: Any) -> Sequence[Any]:
            return [FakeTool(name="nexus_read_file")]

        # First call at revision 0 (bucket 0)
        await mw.on_list_tools(ctx, call_next)  # type: ignore[arg-type]
        assert mw._cache_misses == 1

        # Change revision to a new bucket
        rebac.get_zone_revision.return_value = 15  # bucket = 15 // 10 = 1
        await mw.on_list_tools(ctx, call_next)  # type: ignore[arg-type]
        assert mw._cache_misses == 2  # New bucket → cache miss

    def test_explicit_invalidation(self):
        mw = make_middleware(granted_tools=["nexus_read_file"])

        # Populate cache
        subject = ("agent", "A")
        mw._get_visible_tools(subject)
        assert len(mw._tool_cache) == 1

        # Invalidate
        mw.invalidate(subject)
        assert len(mw._tool_cache) == 0

    def test_invalidate_all(self):
        mw = make_middleware(granted_tools=["nexus_read_file"])

        # Populate cache for two subjects
        mw._get_visible_tools(("agent", "A"))
        mw._get_visible_tools(("agent", "B"))
        assert len(mw._tool_cache) >= 1  # At least 1 (may be 2 if different keys)

        mw.invalidate()
        assert len(mw._tool_cache) == 0


# ---------------------------------------------------------------------------
# Subject extraction
# ---------------------------------------------------------------------------


class TestSubjectExtraction:
    def test_extract_from_state(self):
        mw = make_middleware()
        ctx = FakeMiddlewareContext(
            fastmcp_context=FakeContext({"subject_type": "agent", "subject_id": "bot-1"}),
        )
        subject = mw._extract_subject(ctx)  # type: ignore[arg-type]
        assert subject == ("agent", "bot-1")

    def test_extract_falls_back_to_api_key(self):
        mw = make_middleware()
        ctx = FakeMiddlewareContext(
            fastmcp_context=FakeContext({"api_key": "sk-test-123"}),
        )
        subject = mw._extract_subject(ctx)  # type: ignore[arg-type]
        assert subject == ("api_key", "sk-test-123")

    def test_extract_returns_none_when_no_context(self):
        mw = make_middleware()
        ctx = FakeMiddlewareContext(fastmcp_context=None)
        subject = mw._extract_subject(ctx)  # type: ignore[arg-type]
        assert subject is None

    def test_extract_returns_none_when_no_identity(self):
        mw = make_middleware()
        ctx = FakeMiddlewareContext(
            fastmcp_context=FakeContext({}),
        )
        subject = mw._extract_subject(ctx)  # type: ignore[arg-type]
        assert subject is None


class TestResolveVisibleTools:
    """Tests for the public resolve_visible_tools() API (#1A DRY fix)."""

    def test_returns_visible_tools_for_subject(self):
        mw = make_middleware(granted_tools=["nexus_read_file", "nexus_list_files"])
        ctx = FakeContext({"subject_type": "agent", "subject_id": "A"})
        result = mw.resolve_visible_tools(ctx)
        assert result == frozenset(["nexus_read_file", "nexus_list_files"])

    def test_returns_none_for_none_ctx(self):
        mw = make_middleware(granted_tools=["nexus_read_file"])
        assert mw.resolve_visible_tools(None) is None

    def test_returns_none_for_no_subject(self):
        mw = make_middleware(granted_tools=["nexus_read_file"])
        ctx = FakeContext({})
        assert mw.resolve_visible_tools(ctx) is None

    def test_falls_back_to_api_key(self):
        mw = make_middleware(granted_tools=["nexus_read_file"])
        ctx = FakeContext({"api_key": "sk-test"})
        result = mw.resolve_visible_tools(ctx)
        assert result is not None
        assert "nexus_read_file" in result

    def test_ctx_without_get_state_returns_none(self):
        mw = make_middleware(granted_tools=["nexus_read_file"])
        result = mw.resolve_visible_tools("not-a-context")  # type: ignore[arg-type]
        assert result is None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class TestMetrics:
    @pytest.mark.asyncio
    async def test_metrics_reflect_operations(self):
        mw = make_middleware(granted_tools=["nexus_read_file"])

        ctx = FakeMiddlewareContext(
            fastmcp_context=FakeContext({"subject_type": "agent", "subject_id": "A"}),
        )

        async def call_next(context: Any) -> Sequence[Any]:
            return [FakeTool(name="nexus_read_file"), FakeTool(name="nexus_write_file")]

        await mw.on_list_tools(ctx, call_next)  # type: ignore[arg-type]

        metrics = mw.metrics
        assert metrics["cache_misses"] == 1
        assert metrics["tools_filtered"] == 1  # nexus_write_file filtered
        assert metrics["enabled"] is True

    def test_initial_metrics_are_zero(self):
        mw = make_middleware()
        metrics = mw.metrics
        assert metrics["cache_hits"] == 0
        assert metrics["cache_misses"] == 0
        assert metrics["tools_filtered"] == 0
        assert metrics["calls_rejected"] == 0
        assert metrics["rebac_errors"] == 0


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------


class TestPerformance:
    @pytest.mark.asyncio
    async def test_filtering_50_tools_under_5ms(self):
        """Filtering 50 tools should complete in under 5ms (cached path)."""
        tools = [f"tool_{i}" for i in range(50)]
        # Grant only half
        granted = tools[:25]
        mw = make_middleware(granted_tools=granted)

        all_tools = [FakeTool(name=t) for t in tools]
        ctx = FakeMiddlewareContext(
            fastmcp_context=FakeContext({"subject_type": "agent", "subject_id": "A"}),
        )

        async def call_next(context: Any) -> Sequence[Any]:
            return all_tools

        # Warm up cache
        await mw.on_list_tools(ctx, call_next)  # type: ignore[arg-type]

        # Measure cached path
        start = time.perf_counter()
        result = await mw.on_list_tools(ctx, call_next)  # type: ignore[arg-type]
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert len(result) == 25
        assert elapsed_ms < 5.0, f"Filtering took {elapsed_ms:.2f}ms, expected <5ms"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_rebac_error_returns_empty_tool_set(self):
        """ReBAC failure should fail-closed (no tools visible)."""
        rebac = MagicMock()
        rebac.rebac_list_objects.side_effect = RuntimeError("DB down")
        rebac.get_zone_revision.return_value = 0

        mw = ToolNamespaceMiddleware(rebac_manager=rebac)

        ctx = FakeMiddlewareContext(
            fastmcp_context=FakeContext({"subject_type": "agent", "subject_id": "A"}),
        )

        async def call_next(context: Any) -> Sequence[Any]:
            return [FakeTool(name="nexus_read_file")]

        result = await mw.on_list_tools(ctx, call_next)  # type: ignore[arg-type]
        assert result == []  # Fail-closed: no tools visible
        assert mw.metrics["rebac_errors"] == 1

    @pytest.mark.asyncio
    async def test_non_tool_objects_ignored(self):
        """Only /tools/ paths should be extracted, not regular file paths."""
        objects = [
            ("file", "/tools/nexus_read_file"),
            ("file", "/workspace/data.txt"),
            ("file", "/admin/config.yaml"),
        ]
        rebac = make_rebac_mock(granted_objects=objects)
        mw = ToolNamespaceMiddleware(rebac_manager=rebac)

        tools = mw._get_visible_tools(("agent", "A"))
        assert tools == frozenset(["nexus_read_file"])
