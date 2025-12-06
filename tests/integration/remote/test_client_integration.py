"""Unit tests for RemoteNexusFS client."""

from unittest.mock import Mock, patch

import httpx
import pytest

from nexus.core.exceptions import NexusError, NexusFileNotFoundError, NexusPermissionError
from nexus.remote.client import RemoteNexusFS
from nexus.server.protocol import RPCErrorCode


class TestRemoteNexusFS:
    """Tests for RemoteNexusFS client."""

    @pytest.fixture
    def mock_client(self):
        """Create mock httpx client."""
        client = Mock(spec=httpx.Client)
        client.headers = {}
        return client

    @pytest.fixture
    def client(self, mock_client):
        """Create RemoteNexusFS client with mocked httpx client."""
        with patch("nexus.remote.client.httpx.Client", return_value=mock_client):
            client = RemoteNexusFS("http://localhost:8080", api_key="test-key")
            client.session = mock_client
            return client

    def test_initialization(self):
        """Test client initialization."""
        client = RemoteNexusFS("http://localhost:8080", api_key="my-secret", timeout=60)
        assert client.server_url == "http://localhost:8080"
        assert client.api_key == "my-secret"
        assert client.timeout == 60
        assert "Authorization" in client.session.headers
        assert client.session.headers["Authorization"] == "Bearer my-secret"

    def test_initialization_without_api_key(self):
        """Test client initialization without API key."""
        client = RemoteNexusFS("http://localhost:8080")
        assert client.api_key is None
        assert "Authorization" not in client.session.headers

    def test_call_rpc_success(self, client, mock_client):
        """Test successful RPC call."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = b'{"jsonrpc":"2.0","id":"123","result":{"exists":true}}'
        mock_client.post.return_value = mock_response

        result = client._call_rpc("exists", {"path": "/test.txt"})
        assert result == {"exists": True}

    def test_call_rpc_network_error(self, client, mock_client):
        """Test RPC call with network error."""
        mock_client.post.side_effect = httpx.HTTPError("Connection failed")

        with pytest.raises(NexusError, match="Network error"):
            client._call_rpc("read", {"path": "/test.txt"})

    def test_call_rpc_http_error(self, client, mock_client):
        """Test RPC call with HTTP error."""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_client.post.return_value = mock_response

        with pytest.raises(NexusError, match="HTTP 500"):
            client._call_rpc("read", {"path": "/test.txt"})

    def test_handle_rpc_error_file_not_found(self, client):
        """Test handling file not found error."""
        error = {
            "code": RPCErrorCode.FILE_NOT_FOUND.value,
            "message": "File not found",
            "data": {"path": "/missing.txt"},
        }

        with pytest.raises(NexusFileNotFoundError):
            client._handle_rpc_error(error)

    def test_handle_rpc_error_permission(self, client):
        """Test handling permission error."""
        error = {"code": RPCErrorCode.PERMISSION_ERROR.value, "message": "Permission denied"}

        with pytest.raises(NexusPermissionError):
            client._handle_rpc_error(error)

    def test_handle_rpc_error_generic(self, client):
        """Test handling generic error."""
        error = {"code": -99999, "message": "Unknown error"}

        with pytest.raises(NexusError, match="RPC error"):
            client._handle_rpc_error(error)

    def test_read(self, client, mock_client):
        """Test read method."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = (
            b'{"jsonrpc":"2.0","id":"1","result":{"__type__":"bytes","data":"dGVzdA=="}}'
        )
        mock_client.post.return_value = mock_response

        result = client.read("/test.txt")
        assert result == b"test"

    def test_write(self, client, mock_client):
        """Test write method."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = b'{"jsonrpc":"2.0","id":"2","result":{"success":true}}'
        mock_client.post.return_value = mock_response

        client.write("/test.txt", b"data")
        # Should not raise exception

    def test_exists(self, client, mock_client):
        """Test exists method."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = b'{"jsonrpc":"2.0","id":"3","result":{"exists":true}}'
        mock_client.post.return_value = mock_response

        result = client.exists("/test.txt")
        assert result is True

    def test_list(self, client, mock_client):
        """Test list method."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = (
            b'{"jsonrpc":"2.0","id":"4","result":{"files":["/file1.txt","/file2.txt"]}}'
        )
        mock_client.post.return_value = mock_response

        result = client.list("/workspace")
        assert result == ["/file1.txt", "/file2.txt"]

    def test_glob(self, client, mock_client):
        """Test glob method."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = b'{"jsonrpc":"2.0","id":"5","result":{"matches":["/*.py"]}}'
        mock_client.post.return_value = mock_response

        result = client.glob("*.py", "/")
        assert result == ["/*.py"]

    def test_mkdir(self, client, mock_client):
        """Test mkdir method."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = b'{"jsonrpc":"2.0","id":"6","result":{"success":true}}'
        mock_client.post.return_value = mock_response

        client.mkdir("/newdir", parents=True, exist_ok=True)
        # Should not raise exception

    def test_is_directory(self, client, mock_client):
        """Test is_directory method."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = b'{"jsonrpc":"2.0","id":"7","result":{"is_directory":true}}'
        mock_client.post.return_value = mock_response

        result = client.is_directory("/workspace")
        assert result is True

    def test_close(self, client, mock_client):
        """Test close method."""
        client.close()
        mock_client.close.assert_called_once()


@pytest.mark.integration
class TestRemoteNexusFSIntegration:
    """Integration tests for RemoteNexusFS with real server."""

    @pytest.fixture
    def server_and_client(self, tmp_path):
        """Start server and create client."""
        import nexus
        from nexus.server import NexusRPCServer

        # Create filesystem
        data_dir = tmp_path / "server-data"
        nx = nexus.connect(
            config={
                "data_dir": str(data_dir),
                "enforce_permissions": False,  # Disable permissions for testing
            }
        )
        nx.mkdir("/test", exist_ok=True)
        nx.write("/test/file.txt", b"test content")

        # Start server in separate thread
        import threading

        server = NexusRPCServer(nx, host="127.0.0.1", port=0, api_key="test-secret")
        # Get actual port
        port = server.server.server_address[1]

        server_thread = threading.Thread(target=server.server.serve_forever, daemon=True)
        server_thread.start()

        # Create client
        client = RemoteNexusFS(f"http://127.0.0.1:{port}", api_key="test-secret", timeout=5)

        yield server, client, nx

        # Cleanup
        client.close()
        server.shutdown()
        nx.close()

    def test_end_to_end_operations(self, server_and_client):
        """Test end-to-end operations with real server and client."""
        server, client, nx = server_and_client

        # Test exists
        assert client.exists("/test/file.txt") is True
        assert client.exists("/nonexistent.txt") is False

        # Test read
        content = client.read("/test/file.txt")
        assert content == b"test content"

        # Test write
        client.write("/test/new.txt", b"new content")
        assert client.exists("/test/new.txt") is True

        # Test list
        files = client.list("/test", recursive=False, details=False)
        assert "/test/file.txt" in files
        assert "/test/new.txt" in files

        # Test is_directory
        assert client.is_directory("/test") is True
        assert client.is_directory("/test/file.txt") is False
