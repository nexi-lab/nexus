"""Tests for MCP server implementation."""

from unittest.mock import AsyncMock, Mock, patch

from nexus.bricks.mcp.server import create_mcp_server


class TestCreateMCPServer:
    """Test create_mcp_server function."""

    async def test_create_server_with_nx_instance(self):
        """Test creating MCP server with NexusFilesystem instance."""
        nx = Mock()
        server = await create_mcp_server(nx=nx, name="test-server")

        assert server is not None
        assert server.name == "test-server"

    async def test_create_server_with_custom_name(self):
        """Test creating MCP server with custom name."""
        nx = Mock()
        server = await create_mcp_server(nx=nx, name="my-custom-server")

        assert server.name == "my-custom-server"

    async def test_create_server_default_name(self):
        """Test creating MCP server with default name."""
        nx = Mock()
        server = await create_mcp_server(nx=nx)

        assert server.name == "nexus"

    async def test_create_server_with_remote_url(self):
        """Test creating MCP server with remote URL."""
        with patch("nexus.connect", new_callable=AsyncMock) as mock_connect:
            mock_instance = Mock()
            mock_connect.return_value = mock_instance

            server = await create_mcp_server(remote_url="http://localhost:2026")

            mock_connect.assert_called_once_with(
                config={"profile": "remote", "url": "http://localhost:2026", "api_key": None}
            )
            assert server is not None

    async def test_create_server_auto_connect(self):
        """Test creating MCP server with auto-connect when nx is None."""
        with patch("nexus.connect", new_callable=AsyncMock) as mock_connect:
            mock_nx = Mock()
            mock_connect.return_value = mock_nx

            server = await create_mcp_server()

            mock_connect.assert_called_once()
            assert server is not None


class TestMCPServerCreation:
    """Test basic MCP server creation."""

    async def test_server_is_created(self):
        """Test that server is successfully created."""
        nx = Mock()
        server = await create_mcp_server(nx=nx)

        assert server is not None
        assert server.name == "nexus"

    async def test_server_with_mock_filesystem(self):
        """Test server creation with fully mocked filesystem."""
        nx = Mock()
        nx.read = Mock(return_value=b"test")
        nx.write = Mock()
        nx.delete = Mock()

        server = await create_mcp_server(nx=nx)

        assert server is not None


class TestMCPMain:
    """Test MCP main entry point."""

    def test_main_imports(self):
        """Test that main function can be imported."""
        from nexus.bricks.mcp.server import main

        assert main is not None
        assert callable(main)
