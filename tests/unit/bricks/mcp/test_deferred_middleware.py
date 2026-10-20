"""Tests for deferred ToolNamespaceMiddleware factory in create_mcp_server.

The factory may pass a zero-arg callable instead of a real middleware
instance to defer the fastmcp/beartype import cost until an MCP server
is actually started. These tests verify:

* Direct-instance pass-through (backward compat)
* Deferred-callable success path (factory invoked once, instance installed)
* A configured middleware must load before the server accepts requests
"""

from unittest.mock import Mock, patch

import pytest

from nexus.bricks.mcp.server import create_mcp_server


@pytest.fixture
def mock_nx():
    nx = Mock()
    nx.read = Mock(return_value=b"")
    nx.write = Mock()
    nx.list = Mock(return_value=[])
    nx.glob = Mock(return_value=[])
    nx.grep = Mock(return_value=[])
    nx.exists = Mock(return_value=True)
    nx.is_directory = Mock(return_value=False)
    nx.edit = Mock(
        return_value={"success": True, "diff": "", "applied_count": 0, "matches": [], "errors": []}
    )
    return nx


def _real_looking_middleware() -> Mock:
    """Mock instance that looks like a real ToolNamespaceMiddleware.

    The detection branch in server.py keys on the ``on_call_tool`` attr
    (every fastmcp Middleware subclass has it), so the test mock must
    expose that attribute to be treated as an instance, not a factory.
    """
    mw = Mock()
    mw.on_call_tool = Mock()  # marker — pass-through path
    mw.resolve_visible_tools = Mock(return_value=None)
    return mw


class TestDeferredMiddleware:
    async def test_direct_instance_passes_through(self, mock_nx):
        """Real middleware instances are not invoked as a callable."""
        instance = _real_looking_middleware()
        server = await create_mcp_server(nx=mock_nx, tool_namespace_middleware=instance)
        assert server is not None
        # Sanity: instance was not called as a factory
        instance.assert_not_called()

    async def test_deferred_callable_materializes_once(self, mock_nx):
        """A zero-arg callable without on_call_tool is invoked exactly once."""
        invocations: list[int] = []

        def factory():
            invocations.append(1)
            return _real_looking_middleware()

        server = await create_mcp_server(nx=mock_nx, tool_namespace_middleware=factory)
        assert server is not None
        assert len(invocations) == 1, "factory must be invoked exactly once"

    async def test_deferred_callable_import_error_refuses_startup(self, mock_nx):
        def bad_factory():
            raise ImportError("simulated missing fastmcp")

        with pytest.raises(ImportError, match="simulated missing fastmcp"):
            await create_mcp_server(nx=mock_nx, tool_namespace_middleware=bad_factory)
        mock_nx.close.assert_not_called()

    async def test_failed_startup_closes_an_owned_filesystem(self, mock_nx):
        def bad_factory():
            raise ImportError("configured middleware unavailable")

        with (
            patch("nexus.connect", return_value=mock_nx),
            pytest.raises(ImportError, match="configured middleware unavailable"),
        ):
            await create_mcp_server(tool_namespace_middleware=bad_factory)
        mock_nx.close.assert_called_once_with()

    @pytest.mark.parametrize("borrowed", [False, True])
    async def test_shutdown_closes_only_an_owned_filesystem(self, mock_nx, borrowed):
        from fastmcp import Client

        with patch("nexus.connect", return_value=mock_nx):
            server = await create_mcp_server(nx=mock_nx if borrowed else None)
        async with Client(server) as client:
            assert await client.list_tools()
            mock_nx.close.assert_not_called()
        if borrowed:
            mock_nx.close.assert_not_called()
        else:
            mock_nx.close.assert_called_once_with()

    async def test_none_passes_through(self, mock_nx):
        """None is the no-op case — server creates without middleware."""
        server = await create_mcp_server(nx=mock_nx, tool_namespace_middleware=None)
        assert server is not None
