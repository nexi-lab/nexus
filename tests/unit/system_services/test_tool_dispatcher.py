"""TDD test scaffolding for ToolDispatcher (Issue #2761, Phase 1).

Tests define the expected behavior of ToolDispatcherProtocol implementations.
Written RED-first: behavioral tests will fail until the concrete implementation
is built in system_services/agent_runtime/.

Contract under test:
    ToolDispatcher.dispatch()          — route tool call to handler
    ToolDispatcher.check_permission()  — access manifest enforcement
    ToolDispatcher.register_handler()  — add tool handlers
    ToolDispatcher.list_tools()        — enumerate registered tools

Linux mapping: sys_call_table — each tool is a "syscall" that agents invoke.

See: src/nexus/contracts/agent_runtime_types.py,
     src/nexus/contracts/access_manifest_types.py
"""

import asyncio

import pytest

from nexus.contracts.agent_runtime_types import (
    ToolDispatcherProtocol,
    ToolNotFoundError,
    ToolPermissionDeniedError,
    ToolResult,
    ToolTimeoutError,
)

# ======================================================================
# Value type tests (pass immediately)
# ======================================================================


class TestToolResult:
    """Verify ToolResult frozen dataclass."""

    def test_success_result(self) -> None:
        result = ToolResult(
            tool_call_id="tc-1",
            name="vfs_read",
            output='{"content": "hello"}',
        )
        assert result.success is True
        assert result.error is None

    def test_error_result(self) -> None:
        result = ToolResult(
            tool_call_id="tc-1",
            name="vfs_read",
            output="",
            error="File not found: /missing.txt",
        )
        assert result.success is False
        assert result.error == "File not found: /missing.txt"

    def test_immutable(self) -> None:
        result = ToolResult(
            tool_call_id="tc-1",
            name="vfs_read",
            output="data",
        )
        attr = "output"
        with pytest.raises(AttributeError):
            setattr(result, attr, "new")

    def test_bytes_output(self) -> None:
        result = ToolResult(
            tool_call_id="tc-1",
            name="vfs_read",
            output=b"\x00\x01\x02",
        )
        assert isinstance(result.output, bytes)

    def test_duration_tracked(self) -> None:
        result = ToolResult(
            tool_call_id="tc-1",
            name="vfs_read",
            output="data",
            duration_ms=42.5,
        )
        assert result.duration_ms == 42.5


class TestToolExceptions:
    """Verify tool exception types."""

    def test_tool_not_found(self) -> None:
        err = ToolNotFoundError("unknown_tool")
        assert err.tool_name == "unknown_tool"
        assert err.is_expected is True
        assert err.status_code == 404

    def test_tool_permission_denied(self) -> None:
        err = ToolPermissionDeniedError("vfs_write", "agent-1")
        assert err.tool_name == "vfs_write"
        assert err.agent_id == "agent-1"
        assert err.is_expected is True
        assert err.status_code == 403

    def test_tool_timeout(self) -> None:
        err = ToolTimeoutError("slow_tool", 30.0)
        assert err.tool_name == "slow_tool"
        assert err.timeout == 30.0
        assert err.is_expected is True
        assert err.status_code == 504


# ======================================================================
# Protocol conformance
# ======================================================================


class TestProtocolConformance:
    """Verify structural typing for ToolDispatcherProtocol."""

    def test_protocol_is_runtime_checkable(self) -> None:
        """ToolDispatcherProtocol is @runtime_checkable."""
        assert hasattr(ToolDispatcherProtocol, "__protocol_attrs__") or hasattr(
            ToolDispatcherProtocol, "__abstractmethods__"
        )


# ======================================================================
# Behavioral tests (RED — need real implementation)
# ======================================================================


class TestToolDispatcherRegistration:
    """Tests for register_handler() and list_tools()."""

    async def test_register_and_list(self) -> None:
        from nexus.system_services.agent_runtime.tool_dispatcher import ToolDispatcher

        dispatcher = ToolDispatcher()

        async def dummy_handler(**kwargs):  # noqa: ARG001
            return "ok"

        dispatcher.register_handler("vfs_read", dummy_handler)
        dispatcher.register_handler("vfs_write", dummy_handler)

        tools = dispatcher.list_tools()
        assert "vfs_read" in tools
        assert "vfs_write" in tools

    async def test_register_duplicate_raises(self) -> None:
        from nexus.system_services.agent_runtime.tool_dispatcher import ToolDispatcher

        dispatcher = ToolDispatcher()

        async def handler(**kwargs):  # noqa: ARG001
            return "ok"

        dispatcher.register_handler("vfs_read", handler)
        with pytest.raises(ValueError, match="already registered"):
            dispatcher.register_handler("vfs_read", handler)

    async def test_list_tools_empty_initially(self) -> None:
        from nexus.system_services.agent_runtime.tool_dispatcher import ToolDispatcher

        dispatcher = ToolDispatcher()
        assert dispatcher.list_tools() == []


class TestToolDispatcherDispatch:
    """Tests for dispatch() — routing tool calls to handlers."""

    async def test_dispatch_calls_handler_and_returns_result(self) -> None:
        from nexus.system_services.agent_runtime.tool_dispatcher import ToolDispatcher

        dispatcher = ToolDispatcher()

        async def read_handler(*, path: str, **kwargs):  # noqa: ARG001
            return f"content of {path}"

        dispatcher.register_handler("vfs_read", read_handler)

        result = await dispatcher.dispatch(
            "vfs_read",
            {"path": "/hello.txt"},
            agent_id="agent-1",
            zone_id="zone-1",
        )

        assert isinstance(result, ToolResult)
        assert result.success is True
        assert "content of /hello.txt" in str(result.output)
        assert result.name == "vfs_read"
        assert result.duration_ms >= 0

    async def test_dispatch_unknown_tool_raises(self) -> None:
        from nexus.system_services.agent_runtime.tool_dispatcher import ToolDispatcher

        dispatcher = ToolDispatcher()
        with pytest.raises(ToolNotFoundError):
            await dispatcher.dispatch(
                "nonexistent",
                {},
                agent_id="agent-1",
                zone_id="zone-1",
            )

    async def test_dispatch_handler_error_captured_in_result(self) -> None:
        """Handler exceptions become ToolResult.error, not propagated."""
        from nexus.system_services.agent_runtime.tool_dispatcher import ToolDispatcher

        dispatcher = ToolDispatcher()

        async def failing_handler(**kwargs):  # noqa: ARG001
            msg = "disk full"
            raise OSError(msg)

        dispatcher.register_handler("vfs_write", failing_handler)

        result = await dispatcher.dispatch(
            "vfs_write",
            {"path": "/file.txt", "content": b"data"},
            agent_id="agent-1",
            zone_id="zone-1",
        )

        assert result.success is False
        assert "disk full" in result.error

    async def test_dispatch_tracks_tool_call_id(self) -> None:
        from nexus.system_services.agent_runtime.tool_dispatcher import ToolDispatcher

        dispatcher = ToolDispatcher()

        async def handler(**kwargs):  # noqa: ARG001
            return "ok"

        dispatcher.register_handler("vfs_read", handler)

        result = await dispatcher.dispatch(
            "vfs_read",
            {},
            agent_id="agent-1",
            zone_id="zone-1",
            tool_call_id="tc-custom-123",
        )

        assert result.tool_call_id == "tc-custom-123"


class TestToolDispatcherPermissions:
    """Tests for permission enforcement via access manifests."""

    async def test_permission_denied_blocks_dispatch(self) -> None:
        """Dispatch raises ToolPermissionDeniedError when access manifest denies."""
        from nexus.contracts.access_manifest_types import (
            AccessManifest,
            ManifestEntry,
            ToolPermission,
        )
        from nexus.system_services.agent_runtime.tool_dispatcher import ToolDispatcher

        dispatcher = ToolDispatcher()

        async def handler(**kwargs):  # noqa: ARG001
            return "ok"

        dispatcher.register_handler("vfs_write", handler)

        # Set deny manifest for agent-restricted
        manifest = AccessManifest(
            id="m-1",
            agent_id="agent-restricted",
            zone_id="zone-1",
            name="deny-write",
            entries=(ManifestEntry(tool_pattern="vfs_write", permission=ToolPermission.DENY),),
            status="active",
            valid_from="2024-01-01T00:00:00Z",
            valid_until=None,
            created_by="admin",
        )
        dispatcher.set_manifest("agent-restricted", manifest)

        with pytest.raises(ToolPermissionDeniedError):
            await dispatcher.dispatch(
                "vfs_write",
                {},
                agent_id="agent-restricted",
                zone_id="zone-1",
            )

    async def test_check_permission_returns_bool(self) -> None:
        from nexus.system_services.agent_runtime.tool_dispatcher import ToolDispatcher

        dispatcher = ToolDispatcher()

        async def handler(**kwargs):  # noqa: ARG001
            return "ok"

        dispatcher.register_handler("vfs_read", handler)

        # Default: all tools allowed (no manifest = permissive)
        result = await dispatcher.check_permission("vfs_read", agent_id="agent-1", zone_id="zone-1")
        assert result is True


class TestToolDispatcherTimeout:
    """Tests for tool execution timeout enforcement."""

    async def test_slow_tool_raises_timeout(self) -> None:
        from nexus.system_services.agent_runtime.tool_dispatcher import ToolDispatcher

        dispatcher = ToolDispatcher(default_timeout=0.05)

        async def slow_handler(**kwargs):  # noqa: ARG001
            await asyncio.sleep(10)
            return "never"

        dispatcher.register_handler("slow_tool", slow_handler)

        with pytest.raises(ToolTimeoutError):
            await dispatcher.dispatch(
                "slow_tool",
                {},
                agent_id="agent-1",
                zone_id="zone-1",
            )

    async def test_fast_tool_completes_within_timeout(self) -> None:
        from nexus.system_services.agent_runtime.tool_dispatcher import ToolDispatcher

        dispatcher = ToolDispatcher(default_timeout=5.0)

        async def fast_handler(**kwargs):  # noqa: ARG001
            return "quick"

        dispatcher.register_handler("fast_tool", fast_handler)

        result = await dispatcher.dispatch(
            "fast_tool",
            {},
            agent_id="agent-1",
            zone_id="zone-1",
        )
        assert result.success is True


class TestToolDispatcherParallel:
    """Tests for parallel tool dispatch (multiple concurrent calls)."""

    async def test_parallel_dispatch(self) -> None:
        """Multiple tool calls dispatched concurrently complete independently."""
        from nexus.system_services.agent_runtime.tool_dispatcher import ToolDispatcher

        dispatcher = ToolDispatcher()
        call_order: list[str] = []

        async def handler_a(**kwargs):  # noqa: ARG001
            await asyncio.sleep(0.02)
            call_order.append("a")
            return "result-a"

        async def handler_b(**kwargs):  # noqa: ARG001
            await asyncio.sleep(0.01)
            call_order.append("b")
            return "result-b"

        dispatcher.register_handler("tool_a", handler_a)
        dispatcher.register_handler("tool_b", handler_b)

        results = await asyncio.gather(
            dispatcher.dispatch("tool_a", {}, agent_id="a1", zone_id="z1"),
            dispatcher.dispatch("tool_b", {}, agent_id="a1", zone_id="z1"),
        )

        assert len(results) == 2
        assert all(r.success for r in results)
        # tool_b should finish first (shorter sleep)
        assert call_order == ["b", "a"]
