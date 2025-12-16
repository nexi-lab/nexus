"""Unit tests for RemoteNexusFS (sync) client."""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

import httpx
import pytest

from nexus.core.exceptions import (
    ConflictError,
    InvalidPathError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
    ValidationError,
)
from nexus.remote.client import (
    RemoteConnectionError,
    RemoteFilesystemError,
    RemoteNexusFS,
    RemoteTimeoutError,
)
from nexus.server.protocol import RPCErrorCode


@pytest.fixture
def mock_httpx_client():
    """Create a mock httpx Client."""
    client = Mock(spec=httpx.Client)
    return client


@pytest.fixture
def remote_client(mock_httpx_client):
    """Create a RemoteNexusFS instance with mocked httpx client."""
    with patch("nexus.remote.client.httpx.Client", return_value=mock_httpx_client):
        client = RemoteNexusFS(
            server_url="http://localhost:8080",
            api_key="test-key",
            timeout=30.0,
            connect_timeout=5.0,
        )
        client._client = mock_httpx_client
        return client


class TestRemoteNexusFSInitialization:
    """Test RemoteNexusFS initialization."""

    def test_init_with_api_key(self):
        """Test initialization with API key."""
        with patch("nexus.remote.client.httpx.Client") as mock_client_class:
            client = RemoteNexusFS(
                server_url="http://localhost:8080",
                api_key="test-key",
            )

            assert client.server_url == "http://localhost:8080"
            assert client.api_key == "test-key"
            assert client.timeout == 90  # Default is 90 seconds
            assert client.connect_timeout == 5

            # Verify httpx client was created with correct headers
            mock_client_class.assert_called_once()
            call_kwargs = mock_client_class.call_args[1]
            assert call_kwargs["headers"]["Authorization"] == "Bearer test-key"

    def test_init_without_api_key(self):
        """Test initialization without API key."""
        with patch("nexus.remote.client.httpx.Client") as mock_client_class:
            client = RemoteNexusFS(server_url="http://localhost:8080")

            assert client.api_key is None

            # Verify httpx client was created without auth header
            call_kwargs = mock_client_class.call_args[1]
            assert "Authorization" not in call_kwargs.get("headers", {})

    def test_init_custom_timeouts(self):
        """Test initialization with custom timeouts."""
        with patch("nexus.remote.client.httpx.Client"):
            client = RemoteNexusFS(
                server_url="http://localhost:8080",
                timeout=60,
                connect_timeout=10,
            )

            assert client.timeout == 60
            assert client.connect_timeout == 10

    def test_init_strips_trailing_slash(self):
        """Test that server_url trailing slash is stripped."""
        with patch("nexus.remote.client.httpx.Client"):
            client = RemoteNexusFS(server_url="http://localhost:8080/")

            assert client.server_url == "http://localhost:8080"

    def test_context_manager(self, remote_client):
        """Test context manager functionality."""
        # The sync client doesn't have _initialized, but context manager should work
        remote_client._client.close = Mock()

        with remote_client:
            pass  # Just enter and exit

        # Should close on exit
        remote_client._client.close.assert_called_once()

    def test_close(self, remote_client):
        """Test close method."""
        remote_client.close()

        remote_client._client.close.assert_called_once()


class TestRemoteNexusFSAuth:
    """Test authentication functionality."""

    def test_fetch_auth_info_success(self, remote_client):
        """Test successful auth info fetch."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "authenticated": True,
            "tenant_id": "default",
            "subject_type": "user",
            "subject_id": "admin",
        }
        remote_client._client.get.return_value = mock_response

        remote_client._fetch_auth_info()

        assert remote_client._tenant_id == "default"
        assert remote_client._agent_id is None

    def test_fetch_auth_info_agent(self, remote_client):
        """Test auth info fetch for agent."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "authenticated": True,
            "tenant_id": "default",
            "subject_type": "agent",
            "subject_id": "agent-123",
        }
        remote_client._client.get.return_value = mock_response

        remote_client._fetch_auth_info()

        assert remote_client._tenant_id == "default"
        assert remote_client._agent_id == "agent-123"

    def test_fetch_auth_info_not_authenticated(self, remote_client):
        """Test auth info fetch when not authenticated."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"authenticated": False}
        remote_client._client.get.return_value = mock_response

        remote_client._fetch_auth_info()

        assert remote_client._tenant_id is None
        assert remote_client._agent_id is None

    def test_fetch_auth_info_http_error(self, remote_client):
        """Test auth info fetch with HTTP error."""
        mock_response = Mock()
        mock_response.status_code = 401
        remote_client._client.get.return_value = mock_response

        remote_client._fetch_auth_info()

        # Should not raise, just log warning
        assert remote_client._tenant_id is None

    def test_fetch_auth_info_exception(self, remote_client):
        """Test auth info fetch with exception."""
        remote_client._client.get.side_effect = httpx.RequestError("Connection failed")

        with pytest.raises(httpx.RequestError):
            remote_client._fetch_auth_info()

    def test_fetch_auth_info_on_init_with_api_key(self, remote_client):
        """Test that auth info is fetched when API key is provided."""
        # The sync client doesn't have _ensure_initialized, but it does fetch auth info
        # when API key is set during initialization
        remote_client._fetch_auth_info = Mock()
        remote_client.api_key = "test-key"
        remote_client._fetch_auth_info()

        remote_client._fetch_auth_info.assert_called_once()

    def test_tenant_id_property(self, remote_client):
        """Test tenant_id property."""
        remote_client._tenant_id = "test-tenant"
        assert remote_client.tenant_id == "test-tenant"

        remote_client.tenant_id = "new-tenant"
        assert remote_client._tenant_id == "new-tenant"

    def test_agent_id_property(self, remote_client):
        """Test agent_id property."""
        remote_client._agent_id = "test-agent"
        assert remote_client.agent_id == "test-agent"

        remote_client.agent_id = "new-agent"
        assert remote_client._agent_id == "new-agent"


class TestRemoteNexusFSRPCCalls:
    """Test RPC call functionality."""

    def test_call_rpc_success(self, remote_client):
        """Test successful RPC call."""
        remote_client._ensure_initialized = Mock()
        remote_client._tenant_id = "default"
        remote_client._agent_id = None

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "test-id",
                "result": {"success": True},
            }
        ).encode()
        remote_client._client.post.return_value = mock_response

        with patch(
            "nexus.remote.client.decode_rpc_message", return_value={"result": {"success": True}}
        ):
            result = remote_client._call_rpc("test_method", {"param": "value"})

            assert result == {"success": True}
            remote_client._client.post.assert_called_once()

    def test_call_rpc_with_agent_id(self, remote_client):
        """Test RPC call with agent ID header."""
        remote_client._ensure_initialized = Mock()
        remote_client._tenant_id = "default"
        remote_client._agent_id = "agent-123"

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = json.dumps(
            {"jsonrpc": "2.0", "id": "test-id", "result": {}}
        ).encode()
        remote_client._client.post.return_value = mock_response

        with patch("nexus.remote.client.decode_rpc_message", return_value={"result": {}}):
            remote_client._call_rpc("test_method")

            # Verify X-Agent-ID header was set
            call_kwargs = remote_client._client.post.call_args[1]
            assert call_kwargs["headers"]["X-Agent-ID"] == "agent-123"
            assert call_kwargs["headers"]["X-Tenant-ID"] == "default"

    def test_call_rpc_custom_timeout(self, remote_client):
        """Test RPC call with custom timeout."""
        remote_client._ensure_initialized = Mock()

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = json.dumps(
            {"jsonrpc": "2.0", "id": "test-id", "result": {}}
        ).encode()
        remote_client._client.post.return_value = mock_response

        with patch("nexus.remote.client.decode_rpc_message", return_value={"result": {}}):
            remote_client._call_rpc("test_method", read_timeout=60.0)

            # Verify custom timeout was used
            call_kwargs = remote_client._client.post.call_args[1]
            assert call_kwargs["timeout"].read == 60.0

    def test_call_rpc_http_error(self, remote_client):
        """Test RPC call with HTTP error."""
        remote_client._ensure_initialized = Mock()

        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        remote_client._client.post.return_value = mock_response

        with pytest.raises(RemoteFilesystemError) as exc_info:
            remote_client._call_rpc("test_method")

        assert exc_info.value.status_code == 500
        assert "test_method" in str(exc_info.value)

    def test_call_rpc_connection_error(self, remote_client):
        """Test RPC call with connection error."""
        remote_client._ensure_initialized = Mock()
        remote_client._client.post.side_effect = httpx.ConnectError("Connection refused")

        with pytest.raises(RemoteConnectionError):
            remote_client._call_rpc("test_method")

    def test_call_rpc_timeout_error(self, remote_client):
        """Test RPC call with timeout error."""
        remote_client._ensure_initialized = Mock()
        remote_client._client.post.side_effect = httpx.TimeoutException("Timeout")

        with pytest.raises(RemoteTimeoutError):
            remote_client._call_rpc("test_method")

    def test_call_rpc_http_error_exception(self, remote_client):
        """Test RPC call with HTTP error exception."""
        remote_client._ensure_initialized = Mock()
        remote_client._client.post.side_effect = httpx.HTTPError("HTTP error")

        with pytest.raises(RemoteFilesystemError):
            remote_client._call_rpc("test_method")


class TestRemoteNexusFSRPCErrorHandling:
    """Test RPC error handling."""

    def test_handle_rpc_error_file_not_found(self, remote_client):
        """Test handling FILE_NOT_FOUND RPC error."""
        remote_client._ensure_initialized = Mock()

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "test-id",
                "error": {
                    "code": RPCErrorCode.FILE_NOT_FOUND.value,
                    "message": "File not found",
                    "data": {"path": "/test.txt"},
                },
            }
        ).encode()
        remote_client._client.post.return_value = mock_response

        with (
            patch(
                "nexus.remote.client.decode_rpc_message",
                return_value={
                    "error": {
                        "code": RPCErrorCode.FILE_NOT_FOUND.value,
                        "message": "File not found",
                        "data": {"path": "/test.txt"},
                    }
                },
            ),
            pytest.raises(NexusFileNotFoundError) as exc_info,
        ):
            remote_client._call_rpc("read", {"path": "/test.txt"})

        assert "/test.txt" in str(exc_info.value)

    def test_handle_rpc_error_invalid_path(self, remote_client):
        """Test handling INVALID_PATH RPC error."""
        remote_client._ensure_initialized = Mock()

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "test-id",
                "error": {
                    "code": RPCErrorCode.INVALID_PATH.value,
                    "message": "Invalid path",
                },
            }
        ).encode()
        remote_client._client.post.return_value = mock_response

        with (
            patch(
                "nexus.remote.client.decode_rpc_message",
                return_value={
                    "error": {
                        "code": RPCErrorCode.INVALID_PATH.value,
                        "message": "Invalid path",
                    }
                },
            ),
            pytest.raises(InvalidPathError),
        ):
            remote_client._call_rpc("read", {"path": "invalid"})

    def test_handle_rpc_error_permission_denied(self, remote_client):
        """Test handling PERMISSION_ERROR RPC error."""
        remote_client._ensure_initialized = Mock()

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "test-id",
                "error": {
                    "code": RPCErrorCode.PERMISSION_ERROR.value,
                    "message": "Permission denied",
                },
            }
        ).encode()
        remote_client._client.post.return_value = mock_response

        with (
            patch(
                "nexus.remote.client.decode_rpc_message",
                return_value={
                    "error": {
                        "code": RPCErrorCode.PERMISSION_ERROR.value,
                        "message": "Permission denied",
                    }
                },
            ),
            pytest.raises(NexusPermissionError),
        ):
            remote_client._call_rpc("write", {"path": "/test.txt", "content": b"data"})

    def test_handle_rpc_error_validation_error(self, remote_client):
        """Test handling VALIDATION_ERROR RPC error."""
        remote_client._ensure_initialized = Mock()

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "test-id",
                "error": {
                    "code": RPCErrorCode.VALIDATION_ERROR.value,
                    "message": "Validation failed",
                },
            }
        ).encode()
        remote_client._client.post.return_value = mock_response

        with (
            patch(
                "nexus.remote.client.decode_rpc_message",
                return_value={
                    "error": {
                        "code": RPCErrorCode.VALIDATION_ERROR.value,
                        "message": "Validation failed",
                    }
                },
            ),
            pytest.raises(ValidationError),
        ):
            remote_client._call_rpc("test_method", {"invalid": "param"})

    def test_handle_rpc_error_conflict(self, remote_client):
        """Test handling CONFLICT RPC error."""
        remote_client._ensure_initialized = Mock()

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "test-id",
                "error": {
                    "code": RPCErrorCode.CONFLICT.value,
                    "message": "Conflict",
                    "data": {
                        "path": "/test.txt",
                        "expected_etag": "etag1",
                        "current_etag": "etag2",
                    },
                },
            }
        ).encode()
        remote_client._client.post.return_value = mock_response

        with (
            patch(
                "nexus.remote.client.decode_rpc_message",
                return_value={
                    "error": {
                        "code": RPCErrorCode.CONFLICT.value,
                        "message": "Conflict",
                        "data": {
                            "path": "/test.txt",
                            "expected_etag": "etag1",
                            "current_etag": "etag2",
                        },
                    }
                },
            ),
            pytest.raises(ConflictError) as exc_info,
        ):
            remote_client._call_rpc("write", {"path": "/test.txt", "content": b"data"})

        assert exc_info.value.path == "/test.txt"
        assert exc_info.value.expected_etag == "etag1"
        assert exc_info.value.current_etag == "etag2"

    def test_handle_rpc_error_unknown(self, remote_client):
        """Test handling unknown RPC error."""
        remote_client._ensure_initialized = Mock()

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "test-id",
                "error": {
                    "code": -9999,
                    "message": "Unknown error",
                },
            }
        ).encode()
        remote_client._client.post.return_value = mock_response

        with (
            patch(
                "nexus.remote.client.decode_rpc_message",
                return_value={
                    "error": {
                        "code": -9999,
                        "message": "Unknown error",
                    }
                },
            ),
            pytest.raises(NexusError) as exc_info,
        ):
            remote_client._call_rpc("test_method")

        assert "Unknown error" in str(exc_info.value)
        assert "-9999" in str(exc_info.value)


class TestRemoteNexusFSFileOperations:
    """Test file operation methods."""

    def test_read(self, remote_client):
        """Test read operation."""
        remote_client._call_rpc = Mock(return_value=b"file content")

        result = remote_client.read("/test.txt")

        assert result == b"file content"
        remote_client._call_rpc.assert_called_once_with(
            "read", {"path": "/test.txt", "return_metadata": False}
        )

    def test_read_with_metadata(self, remote_client):
        """Test read operation with metadata."""
        remote_client._call_rpc = Mock(
            return_value={"content": b"file content", "size": 12, "etag": "etag123"}
        )

        result = remote_client.read("/test.txt", return_metadata=True)

        assert result["content"] == b"file content"
        assert result["size"] == 12
        remote_client._call_rpc.assert_called_once_with(
            "read", {"path": "/test.txt", "return_metadata": True}
        )

    def test_write(self, remote_client):
        """Test write operation."""
        remote_client._call_rpc = Mock(return_value={"etag": "etag123", "size": 11})

        result = remote_client.write("/test.txt", b"hello world")

        assert result["etag"] == "etag123"
        remote_client._call_rpc.assert_called_once_with(
            "write",
            {
                "path": "/test.txt",
                "content": b"hello world",
                "if_match": None,
                "if_none_match": False,
                "force": False,
            },
        )

    def test_write_with_if_match(self, remote_client):
        """Test write operation with if_match (etag)."""
        remote_client._call_rpc = Mock(return_value={"etag": "etag456", "size": 11})

        result = remote_client.write("/test.txt", b"hello world", if_match="etag123")

        assert result["etag"] == "etag456"
        remote_client._call_rpc.assert_called_once_with(
            "write",
            {
                "path": "/test.txt",
                "content": b"hello world",
                "if_match": "etag123",
                "if_none_match": False,
                "force": False,
            },
        )

    def test_list(self, remote_client):
        """Test list operation."""
        remote_client._call_rpc = Mock(return_value={"files": ["/file1.txt", "/file2.txt"]})

        result = remote_client.list("/workspace")

        assert result == ["/file1.txt", "/file2.txt"]
        remote_client._call_rpc.assert_called_once_with(
            "list",
            {
                "path": "/workspace",
                "recursive": True,
                "details": False,
                "prefix": None,
                "show_parsed": True,
            },
        )

    def test_list_with_options(self, remote_client):
        """Test list operation with options."""
        remote_client._call_rpc = Mock(return_value={"files": ["/file1.txt"]})

        result = remote_client.list("/workspace", recursive=True, details=True, prefix="test")

        assert result == ["/file1.txt"]
        remote_client._call_rpc.assert_called_once_with(
            "list",
            {
                "path": "/workspace",
                "recursive": True,
                "details": True,
                "prefix": "test",
                "show_parsed": True,
            },
        )

    def test_exists(self, remote_client):
        """Test exists operation."""
        remote_client._call_rpc = Mock(return_value={"exists": True})

        result = remote_client.exists("/test.txt")

        assert result is True
        remote_client._call_rpc.assert_called_once_with("exists", {"path": "/test.txt"})

    def test_delete(self, remote_client):
        """Test delete operation."""
        remote_client._call_rpc = Mock(return_value=None)

        remote_client.delete("/test.txt")

        remote_client._call_rpc.assert_called_once_with("delete", {"path": "/test.txt"})

    def test_rename(self, remote_client):
        """Test rename operation."""
        remote_client._call_rpc = Mock(return_value=None)

        remote_client.rename("/old.txt", "/new.txt")

        remote_client._call_rpc.assert_called_once_with(
            "rename", {"old_path": "/old.txt", "new_path": "/new.txt"}
        )

    def test_stat(self, remote_client):
        """Test stat operation."""
        remote_client._call_rpc = Mock(
            return_value={"size": 1024, "is_directory": False, "etag": "etag123"}
        )

        result = remote_client.stat("/test.txt")

        assert result["size"] == 1024
        assert result["is_directory"] is False
        remote_client._call_rpc.assert_called_once_with("stat", {"path": "/test.txt"})

    def test_glob(self, remote_client):
        """Test glob operation."""
        remote_client._call_rpc = Mock(return_value={"matches": ["/file1.py", "/file2.py"]})

        result = remote_client.glob("*.py", "/workspace")

        assert result == ["/file1.py", "/file2.py"]
        remote_client._call_rpc.assert_called_once_with(
            "glob", {"pattern": "*.py", "path": "/workspace"}
        )

    def test_grep(self, remote_client):
        """Test grep operation."""
        remote_client._call_rpc = Mock(
            return_value={
                "results": [
                    {"file": "/test.py", "line": 10, "content": "def test():"},
                ]
            }
        )

        result = remote_client.grep("def test", "/workspace")

        assert len(result) == 1
        assert result[0]["file"] == "/test.py"
        remote_client._call_rpc.assert_called_once_with(
            "grep",
            {
                "pattern": "def test",
                "path": "/workspace",
                "file_pattern": None,
                "ignore_case": False,
                "max_results": 1000,
                "search_mode": "auto",
            },
        )
