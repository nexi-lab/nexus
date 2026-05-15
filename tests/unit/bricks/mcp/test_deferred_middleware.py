"""Tests for deferred ToolNamespaceMiddleware factory in create_mcp_server.

The factory may pass a zero-arg callable instead of a real middleware
instance to defer the fastmcp/beartype import cost until an MCP server
is actually started. These tests verify:

* Direct-instance pass-through (backward compat)
* Deferred-callable success path (factory invoked once, instance installed)
* Deferred-callable ImportError path (graceful degradation, no crash)
"""

import logging
from unittest.mock import Mock

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

    async def test_deferred_callable_import_error_degrades(self, mock_nx, caplog):
        """ImportError from the factory is logged and MCP server still creates.

        Mirrors the prior eager-boot policy in factory/_bricks.py where a
        missing middleware module skipped the optional brick wiring instead
        of failing the whole connect path.
        """

        def bad_factory():
            raise ImportError("simulated missing fastmcp")

        with caplog.at_level(logging.WARNING, logger="nexus.bricks.mcp.server"):
            server = await create_mcp_server(nx=mock_nx, tool_namespace_middleware=bad_factory)

        assert server is not None, "create_mcp_server must not crash on factory ImportError"
        assert any(
            "ToolNamespaceMiddleware unavailable" in rec.message for rec in caplog.records
        ), "expected WARNING about deferred middleware import failure"

    async def test_none_passes_through(self, mock_nx):
        """None is the no-op case — server creates without middleware."""
        server = await create_mcp_server(nx=mock_nx, tool_namespace_middleware=None)
        assert server is not None
