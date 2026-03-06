"""Tests for AgentVFSProtocol / AgentSearchProtocol decoupling (Issue #2761).

Verifies:
    - ToolDispatcher accepts AgentVFSProtocol (not just NexusFS)
    - ToolDispatcher uses AgentSearchProtocol for grep/glob
    - ToolDispatcher falls back to sys_readdir without search
    - ProcessManager accepts AgentVFSProtocol
    - SessionStore accepts AgentVFSProtocol
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from nexus.contracts.protocols.agent_vfs import AgentSearchProtocol, AgentVFSProtocol
from nexus.system_services.agent_runtime.session_store import SessionStore
from nexus.system_services.agent_runtime.tool_dispatcher import ToolDispatcher

# ======================================================================
# Helpers
# ======================================================================


def _make_vfs() -> MagicMock:
    """Create a mock that satisfies AgentVFSProtocol."""
    vfs = MagicMock(spec=AgentVFSProtocol)
    vfs.sys_read = MagicMock(return_value=b"file content")
    vfs.sys_write = MagicMock(return_value=42)
    vfs.sys_access = MagicMock(return_value=True)
    vfs.sys_readdir = MagicMock(return_value=["file1.py", "file2.py"])
    return vfs


def _make_search() -> MagicMock:
    """Create a mock that satisfies AgentSearchProtocol."""
    search = MagicMock(spec=AgentSearchProtocol)
    search.grep = MagicMock(
        return_value=[
            {"file": "/src/main.py", "line": 10, "content": "TODO: fix this"},
        ]
    )
    search.glob = MagicMock(return_value=["/src/main.py", "/src/util.py"])
    return search


def _make_tool_call(name: str, arguments: str = "{}") -> MagicMock:
    tc = MagicMock()
    tc.id = "tc-1"
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = arguments
    return tc


def _make_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.user_id = "test"
    ctx.zone_id = "zone-1"
    return ctx


# ======================================================================
# ToolDispatcher + VFS protocol
# ======================================================================


class TestToolDispatcherVFSProtocol:
    """ToolDispatcher works with any AgentVFSProtocol."""

    def test_tool_dispatcher_accepts_vfs_protocol(self) -> None:
        """Constructor accepts a VFS protocol mock (not NexusFS)."""
        vfs = _make_vfs()
        dispatcher = ToolDispatcher(vfs)
        assert dispatcher._vfs is vfs

    def test_tool_dispatcher_accepts_search_protocol(self) -> None:
        """Constructor accepts separate search protocol."""
        vfs = _make_vfs()
        search = _make_search()
        dispatcher = ToolDispatcher(vfs, search=search)
        assert dispatcher._search is search

    async def test_tool_dispatcher_uses_search_protocol_for_grep(self) -> None:
        """grep dispatches through AgentSearchProtocol, not VFS."""
        vfs = _make_vfs()
        search = _make_search()
        dispatcher = ToolDispatcher(vfs, search=search)
        ctx = _make_ctx()
        tc = _make_tool_call("grep", '{"pattern": "TODO"}')

        result = await dispatcher.dispatch(ctx, tc)

        search.grep.assert_called_once()
        assert "TODO" in result or "match" in result.lower()

    async def test_tool_dispatcher_uses_search_protocol_for_glob(self) -> None:
        """glob dispatches through AgentSearchProtocol."""
        vfs = _make_vfs()
        search = _make_search()
        dispatcher = ToolDispatcher(vfs, search=search)
        ctx = _make_ctx()
        tc = _make_tool_call("glob", '{"pattern": "*.py"}')

        result = await dispatcher.dispatch(ctx, tc)

        search.glob.assert_called_once()
        assert "main.py" in result

    async def test_tool_dispatcher_falls_back_without_search(self) -> None:
        """Without search protocol, grep returns unavailable message."""
        vfs = _make_vfs()
        dispatcher = ToolDispatcher(vfs)  # no search
        ctx = _make_ctx()
        tc = _make_tool_call("grep", '{"pattern": "TODO"}')

        result = await dispatcher.dispatch(ctx, tc)

        assert "unavailable" in result.lower()

    async def test_tool_dispatcher_glob_falls_back_to_readdir(self) -> None:
        """Without search protocol, glob falls back to sys_readdir."""
        vfs = _make_vfs()
        dispatcher = ToolDispatcher(vfs)  # no search
        ctx = _make_ctx()
        tc = _make_tool_call("glob", '{"pattern": "*.py"}')

        result = await dispatcher.dispatch(ctx, tc)

        vfs.sys_readdir.assert_called_once()
        assert "file1.py" in result


# ======================================================================
# ProcessManager + VFS protocol
# ======================================================================


class TestProcessManagerVFSProtocol:
    """ProcessManager works with AgentVFSProtocol."""

    def test_process_manager_accepts_vfs_protocol(self) -> None:
        """Constructor accepts a VFS protocol mock."""
        from nexus.system_services.agent_runtime.process_manager import ProcessManager

        vfs = _make_vfs()
        llm = AsyncMock()
        pm = ProcessManager(vfs, llm)
        assert pm._vfs is vfs

    async def test_process_manager_spawn_uses_vfs(self) -> None:
        """spawn() writes files via VFS protocol."""
        from nexus.system_services.agent_runtime.process_manager import ProcessManager
        from nexus.system_services.agent_runtime.types import AgentProcessConfig

        vfs = _make_vfs()
        llm = AsyncMock()
        pm = ProcessManager(vfs, llm)

        config = AgentProcessConfig(name="test-agent")
        await pm.spawn("owner-1", "zone-1", config=config)

        # Should have called sys_write for SYSTEM.md and settings.json
        assert vfs.sys_write.call_count >= 2


# ======================================================================
# SessionStore + VFS protocol
# ======================================================================


class TestSessionStoreVFSProtocol:
    """SessionStore works with AgentVFSProtocol."""

    def test_session_store_accepts_vfs_protocol(self) -> None:
        """Constructor accepts a VFS protocol mock."""
        vfs = _make_vfs()
        store = SessionStore(vfs)
        assert store._vfs is vfs
