"""Unit tests for RPC server."""

from unittest.mock import Mock, patch

import pytest

from nexus.server.rpc_server import NexusRPCServer, RPCRequestHandler


class TestRPCRequestHandler:
    """Tests for RPCRequestHandler class."""

    @pytest.fixture
    def mock_filesystem(self):
        """Create mock filesystem."""
        fs = Mock()
        fs.read = Mock(return_value=b"test content")
        fs.write = Mock()
        fs.delete = Mock()
        fs.exists = Mock(return_value=True)
        fs.list = Mock(return_value=["/file1.txt", "/file2.txt"])
        fs.glob = Mock(return_value=["/test.py"])
        fs.grep = Mock(return_value=[{"file": "/test.txt", "line": 1, "content": "match"}])
        fs.mkdir = Mock()
        fs.rmdir = Mock()
        fs.is_directory = Mock(return_value=False)
        return fs

    @pytest.fixture
    def mock_handler(self, mock_filesystem):
        """Create a mock handler with necessary attributes set."""
        handler = Mock(spec=RPCRequestHandler)
        handler.nexus_fs = mock_filesystem
        handler.api_key = None
        handler.headers = {}
        # Bind the actual methods to the mock
        handler._validate_auth = lambda: RPCRequestHandler._validate_auth(handler)
        handler._dispatch_method = lambda method, params: RPCRequestHandler._dispatch_method(
            handler, method, params
        )
        return handler

    def test_validate_auth_no_key(self, mock_handler):
        """Test authentication validation when no API key is set."""
        mock_handler.api_key = None
        mock_handler.headers = {}
        assert mock_handler._validate_auth() is True

    def test_validate_auth_with_valid_key(self, mock_handler):
        """Test authentication with valid API key."""
        mock_handler.api_key = "secret123"
        mock_handler.headers = {"Authorization": "Bearer secret123"}
        assert mock_handler._validate_auth() is True

    def test_validate_auth_with_invalid_key(self, mock_handler):
        """Test authentication with invalid API key."""
        mock_handler.api_key = "secret123"
        mock_handler.headers = {"Authorization": "Bearer wrong"}
        assert mock_handler._validate_auth() is False

    def test_validate_auth_missing_header(self, mock_handler):
        """Test authentication with missing header."""
        mock_handler.api_key = "secret123"
        mock_handler.headers = {}
        assert mock_handler._validate_auth() is False

    def test_dispatch_read(self, mock_handler, mock_filesystem):
        """Test dispatching read method."""
        from nexus.server.protocol import ReadParams

        params = ReadParams(path="/test.txt")
        result = mock_handler._dispatch_method("read", params)

        assert result == b"test content"
        mock_filesystem.read.assert_called_once_with("/test.txt")

    def test_dispatch_write(self, mock_handler, mock_filesystem):
        """Test dispatching write method."""
        from nexus.server.protocol import WriteParams

        params = WriteParams(path="/test.txt", content=b"data")
        result = mock_handler._dispatch_method("write", params)

        assert result == {"success": True}
        mock_filesystem.write.assert_called_once_with("/test.txt", b"data")

    def test_dispatch_list(self, mock_handler, mock_filesystem):
        """Test dispatching list method."""
        from nexus.server.protocol import ListParams

        params = ListParams(path="/workspace", recursive=True, details=False)
        result = mock_handler._dispatch_method("list", params)

        assert "files" in result
        assert result["files"] == ["/file1.txt", "/file2.txt"]

    def test_dispatch_exists(self, mock_handler, mock_filesystem):
        """Test dispatching exists method."""
        from nexus.server.protocol import ExistsParams

        params = ExistsParams(path="/test.txt")
        result = mock_handler._dispatch_method("exists", params)

        assert result == {"exists": True}
        mock_filesystem.exists.assert_called_once_with("/test.txt")

    def test_dispatch_unknown_method(self, mock_handler, mock_filesystem):
        """Test dispatching unknown method raises error."""
        with pytest.raises(ValueError, match="Unknown method"):
            mock_handler._dispatch_method("unknown", Mock())


class TestNexusRPCServer:
    """Tests for NexusRPCServer class."""

    @pytest.fixture
    def mock_filesystem(self):
        """Create mock filesystem."""
        fs = Mock()
        fs.close = Mock()
        return fs

    def test_server_initialization(self, mock_filesystem):
        """Test server initialization."""
        server = NexusRPCServer(mock_filesystem, host="127.0.0.1", port=9999, api_key="test")

        assert server.nexus_fs == mock_filesystem
        assert server.host == "127.0.0.1"
        assert server.port == 9999
        assert server.api_key == "test"

    def test_server_shutdown(self, mock_filesystem):
        """Test server shutdown."""
        server = NexusRPCServer(mock_filesystem, host="127.0.0.1", port=9999)

        with (
            patch.object(server.server, "shutdown") as mock_shutdown,
            patch.object(server.server, "server_close") as mock_close,
        ):
            server.shutdown()
            mock_shutdown.assert_called_once()
            mock_close.assert_called_once()
            mock_filesystem.close.assert_called_once()


class TestRPCServerIntegration:
    """Integration tests for RPC server."""

    @pytest.fixture
    def temp_nexus(self, tmp_path):
        """Create temporary Nexus instance."""
        import nexus

        data_dir = tmp_path / "nexus-data"
        nx = nexus.connect(config={"data_dir": str(data_dir)})
        nx.mkdir("/test", exist_ok=True)
        nx.write("/test/file.txt", b"test content")
        yield nx
        nx.close()

    def test_server_with_real_filesystem(self, temp_nexus):
        """Test server with real filesystem."""
        server = NexusRPCServer(temp_nexus, host="127.0.0.1", port=9998, api_key=None)

        # Verify handler is configured
        assert RPCRequestHandler.nexus_fs == temp_nexus
        assert RPCRequestHandler.api_key is None

        # Clean up
        server.server.server_close()
