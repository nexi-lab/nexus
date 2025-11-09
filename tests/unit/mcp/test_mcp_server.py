"""Tests for MCP server implementation."""

from unittest.mock import Mock, patch

from nexus.mcp.server import create_mcp_server


class TestCreateMCPServer:
    """Test create_mcp_server function."""

    def test_create_server_with_nx_instance(self):
        """Test creating MCP server with NexusFilesystem instance."""
        nx = Mock()
        server = create_mcp_server(nx=nx, name="test-server")

        assert server is not None
        assert server.name == "test-server"

    def test_create_server_with_custom_name(self):
        """Test creating MCP server with custom name."""
        nx = Mock()
        server = create_mcp_server(nx=nx, name="my-custom-server")

        assert server.name == "my-custom-server"

    def test_create_server_default_name(self):
        """Test creating MCP server with default name."""
        nx = Mock()
        server = create_mcp_server(nx=nx)

        assert server.name == "nexus"

    def test_create_server_with_remote_url(self):
        """Test creating MCP server with remote URL."""
        with patch("nexus.remote.RemoteNexusFS") as mock_remote_fs:
            mock_instance = Mock()
            mock_remote_fs.return_value = mock_instance

            server = create_mcp_server(remote_url="http://localhost:8080")

            mock_remote_fs.assert_called_once_with("http://localhost:8080", api_key=None)
            assert server is not None

    def test_create_server_auto_connect(self):
        """Test creating MCP server with auto-connect when nx is None."""
        with patch("nexus.connect") as mock_connect:
            mock_nx = Mock()
            mock_connect.return_value = mock_nx

            server = create_mcp_server()

            mock_connect.assert_called_once()
            assert server is not None


class TestMCPServerCreation:
    """Test basic MCP server creation."""

    def test_server_is_created(self):
        """Test that server is successfully created."""
        nx = Mock()
        server = create_mcp_server(nx=nx)

        assert server is not None
        assert server.name == "nexus"

    def test_server_with_mock_filesystem(self):
        """Test server creation with fully mocked filesystem."""
        nx = Mock()
        nx.read = Mock(return_value=b"test")
        nx.write = Mock()
        nx.delete = Mock()

        server = create_mcp_server(nx=nx)

        assert server is not None


class TestMCPMain:
    """Test MCP main entry point."""

    def test_main_imports(self):
        """Test that main function can be imported."""
        from nexus.mcp.server import main

        assert main is not None
        assert callable(main)
