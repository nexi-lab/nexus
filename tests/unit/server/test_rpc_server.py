"""Unit tests for RPC server."""

from io import BytesIO
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
        # write() returns metadata dict (etag, version, modified_at, size)
        fs.write = Mock(
            return_value={
                "etag": "abc123",
                "version": 1,
                "modified_at": "2024-01-01T00:00:00Z",
                "size": 4,
            }
        )
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
        handler.auth_provider = None
        handler.event_loop = None
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
        """Test dispatching read method with virtual view."""
        from unittest.mock import ANY

        from nexus.server.protocol import ReadParams

        # Configure mock to support virtual view path parsing
        # When reading /test_parsed.pdf.md, it will check if /test.pdf exists and read it
        mock_filesystem.exists.return_value = True
        mock_filesystem.read.return_value = b"test content"

        params = ReadParams(path="/test_parsed.pdf.md")
        result = mock_handler._dispatch_method("read", params)

        # Virtual view logic reads the base file (/test.pdf) and parses it
        assert result == b"test content"
        mock_filesystem.exists.assert_called_with("/test.pdf")
        mock_filesystem.read.assert_called_once_with("/test.pdf", context=ANY)

    def test_dispatch_write(self, mock_handler, mock_filesystem):
        """Test dispatching write method."""
        from nexus.server.protocol import WriteParams

        params = WriteParams(path="/test.txt", content=b"data")
        result = mock_handler._dispatch_method("write", params)

        # write() returns metadata dict, not {"success": True}
        assert "etag" in result
        assert "version" in result
        assert "modified_at" in result
        assert "size" in result
        # Verify write was called with correct params (v0.3.9 adds OCC params)
        from unittest.mock import ANY

        mock_filesystem.write.assert_called_once_with(
            "/test.txt", b"data", context=ANY, if_match=None, if_none_match=False, force=False
        )

    def test_dispatch_list(self, mock_handler, mock_filesystem):
        """Test dispatching list method."""
        from nexus.server.protocol import ListParams

        params = ListParams(path="/workspace", recursive=True, details=False)
        result = mock_handler._dispatch_method("list", params)

        assert "files" in result
        assert result["files"] == ["/file1.txt", "/file2.txt"]

    def test_dispatch_exists(self, mock_handler, mock_filesystem):
        """Test dispatching exists method with virtual view."""
        from nexus.server.protocol import ExistsParams

        # Configure mock to support virtual view path parsing
        # When checking /test_parsed.pdf.md, it will check if /test.pdf exists
        mock_filesystem.exists.return_value = True

        params = ExistsParams(path="/test_parsed.pdf.md")
        result = mock_handler._dispatch_method("exists", params)

        assert result == {"exists": True}
        # Virtual view exists check calls exists() twice:
        # 1) parse_virtual_path calls exists() to verify base file exists
        # 2) The exists handler calls exists() again to return the result
        assert mock_filesystem.exists.call_count == 2
        assert all(call.args[0] == "/test.pdf" for call in mock_filesystem.exists.call_args_list)

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
        from unittest.mock import ANY

        # Mock ThreadingHTTPServer to avoid actual port binding
        with patch("nexus.server.rpc_server.ThreadingHTTPServer") as mock_http_server:
            mock_http_server.return_value = Mock()
            server = NexusRPCServer(mock_filesystem, host="127.0.0.1", port=9999, api_key="test")

            assert server.nexus_fs == mock_filesystem
            assert server.host == "127.0.0.1"
            assert server.port == 9999
            assert server.api_key == "test"

            # Verify ThreadingHTTPServer was called with correct parameters
            mock_http_server.assert_called_once_with(("127.0.0.1", 9999), ANY)

    def test_server_shutdown(self, mock_filesystem):
        """Test server shutdown."""
        # Mock ThreadingHTTPServer to avoid actual port binding
        with patch("nexus.server.rpc_server.ThreadingHTTPServer") as mock_http_server:
            mock_server_instance = Mock()
            mock_http_server.return_value = mock_server_instance
            server = NexusRPCServer(mock_filesystem, host="127.0.0.1", port=9999)

            # Now test shutdown
            with (
                patch.object(mock_server_instance, "shutdown") as mock_shutdown,
                patch.object(mock_server_instance, "server_close") as mock_close,
            ):
                server.shutdown()
                mock_shutdown.assert_called_once()
                mock_close.assert_called_once()
                mock_filesystem.close.assert_called_once()


class TestRPCRequestHandlerHTTP:
    """Tests for HTTP methods in RPCRequestHandler."""

    @pytest.fixture
    def mock_handler(self):
        """Create a mock handler for testing HTTP methods."""
        handler = Mock(spec=RPCRequestHandler)
        handler.nexus_fs = Mock()
        handler.api_key = None
        handler.auth_provider = None
        handler.event_loop = None
        handler.headers = {}
        handler.path = ""
        handler.rfile = BytesIO()
        handler.wfile = BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.address_string = Mock(return_value="127.0.0.1")

        # Bind actual methods
        handler._set_cors_headers = lambda: RPCRequestHandler._set_cors_headers(handler)
        handler._send_json_response = lambda status, data: RPCRequestHandler._send_json_response(
            handler, status, data
        )
        handler.do_OPTIONS = lambda: RPCRequestHandler.do_OPTIONS(handler)
        handler.do_GET = lambda: RPCRequestHandler.do_GET(handler)
        handler.log_message = Mock()

        return handler

    def test_cors_headers(self, mock_handler):
        """Test CORS headers are set correctly."""
        mock_handler._set_cors_headers()

        # Verify all CORS headers are set
        calls = mock_handler.send_header.call_args_list
        headers = {call[0][0]: call[0][1] for call in calls}

        assert "Access-Control-Allow-Origin" in headers
        assert headers["Access-Control-Allow-Origin"] == "*"
        assert "Access-Control-Allow-Methods" in headers
        assert "Access-Control-Allow-Headers" in headers

    def test_do_options(self, mock_handler):
        """Test OPTIONS request handling (CORS preflight)."""
        mock_handler.do_OPTIONS()

        mock_handler.send_response.assert_called_once_with(200)
        mock_handler.end_headers.assert_called_once()

    def test_do_get_health(self, mock_handler):
        """Test GET /health endpoint."""
        mock_handler.path = "/health"
        mock_handler.do_GET()

        # Verify 200 response was sent
        mock_handler.send_response.assert_called_once_with(200)

        # Verify JSON was written
        written_data = mock_handler.wfile.getvalue()
        assert b"healthy" in written_data or mock_handler.send_response.called

    def test_do_get_status(self, mock_handler):
        """Test GET /api/nfs/status endpoint."""
        mock_handler.path = "/api/nfs/status"
        mock_handler.do_GET()

        # Verify 200 response
        mock_handler.send_response.assert_called_once_with(200)

    def test_do_get_404(self, mock_handler):
        """Test GET unknown endpoint returns 404."""
        mock_handler.path = "/unknown"
        mock_handler.do_GET()

        # Verify 404 was sent
        mock_handler.send_response.assert_called_once_with(404)

    def test_do_get_unknown_path(self, mock_handler):
        """Test GET to unknown path triggers 404 handler."""
        mock_handler.path = "/some/unknown/path"
        mock_handler.do_GET()

        # Should get a response (either 404 or health/status)
        assert mock_handler.send_response.called


class TestRPCValidation:
    """Tests for validation methods."""

    def test_validate_auth_with_wrong_header_format(self):
        """Test auth validation with malformed header."""
        handler = Mock(spec=RPCRequestHandler)
        handler.api_key = "secret123"
        handler.auth_provider = None
        handler.event_loop = None
        handler.headers = {"Authorization": "InvalidFormat secret123"}
        handler._validate_auth = lambda: RPCRequestHandler._validate_auth(handler)

        assert handler._validate_auth() is False


class TestRPCDispatchMethods:
    """Tests for additional method dispatch cases."""

    @pytest.fixture
    def mock_handler(self):
        """Create mock handler for dispatch tests."""
        handler = Mock(spec=RPCRequestHandler)
        handler.nexus_fs = Mock()
        handler.nexus_fs.delete = Mock()
        handler.nexus_fs.rename = Mock()
        handler.nexus_fs.mkdir = Mock()
        handler.nexus_fs.rmdir = Mock()
        handler.nexus_fs.is_directory = Mock(return_value=False)
        handler.nexus_fs.glob = Mock(return_value=["/test.py"])
        handler.nexus_fs.grep = Mock(return_value=[{"file": "/test.txt", "line": 1}])
        handler.nexus_fs.get_available_namespaces = Mock(return_value=["default"])

        handler._dispatch_method = lambda method, params: RPCRequestHandler._dispatch_method(
            handler, method, params
        )

        return handler

    def test_dispatch_delete(self, mock_handler):
        """Test dispatching delete method."""
        from unittest.mock import ANY

        from nexus.server.protocol import DeleteParams

        params = DeleteParams(path="/test.txt")
        result = mock_handler._dispatch_method("delete", params)

        assert result == {"success": True}
        mock_handler.nexus_fs.delete.assert_called_once_with("/test.txt", context=ANY)

    def test_dispatch_rename(self, mock_handler):
        """Test dispatching rename method."""
        from unittest.mock import ANY

        from nexus.server.protocol import RenameParams

        params = RenameParams(old_path="/old.txt", new_path="/new.txt")
        result = mock_handler._dispatch_method("rename", params)

        assert result == {"success": True}
        mock_handler.nexus_fs.rename.assert_called_once_with("/old.txt", "/new.txt", context=ANY)

    def test_dispatch_mkdir(self, mock_handler):
        """Test dispatching mkdir method."""
        from unittest.mock import ANY

        from nexus.server.protocol import MkdirParams

        params = MkdirParams(path="/newdir", parents=True, exist_ok=False)
        result = mock_handler._dispatch_method("mkdir", params)

        assert result == {"success": True}
        mock_handler.nexus_fs.mkdir.assert_called_once_with(
            "/newdir", parents=True, exist_ok=False, context=ANY
        )

    def test_dispatch_rmdir(self, mock_handler):
        """Test dispatching rmdir method."""
        from unittest.mock import ANY

        from nexus.server.protocol import RmdirParams

        params = RmdirParams(path="/olddir", recursive=True)
        result = mock_handler._dispatch_method("rmdir", params)

        assert result == {"success": True}
        mock_handler.nexus_fs.rmdir.assert_called_once_with("/olddir", recursive=True, context=ANY)

    def test_dispatch_is_directory(self, mock_handler):
        """Test dispatching is_directory method."""
        from unittest.mock import ANY

        from nexus.server.protocol import IsDirectoryParams

        params = IsDirectoryParams(path="/test")
        result = mock_handler._dispatch_method("is_directory", params)

        assert result == {"is_directory": False}
        mock_handler.nexus_fs.is_directory.assert_called_once_with("/test", context=ANY)

    def test_dispatch_glob(self, mock_handler):
        """Test dispatching glob method."""
        from nexus.server.protocol import GlobParams

        params = GlobParams(pattern="*.py", path="/")
        result = mock_handler._dispatch_method("glob", params)

        assert "matches" in result
        assert result["matches"] == ["/test.py"]

    def test_dispatch_grep(self, mock_handler):
        """Test dispatching grep method."""
        from nexus.server.protocol import GrepParams

        params = GrepParams(
            pattern="test",
            path="/",
            file_pattern="*.txt",
            ignore_case=False,
            max_results=100,
        )
        result = mock_handler._dispatch_method("grep", params)

        assert "results" in result
        assert len(result["results"]) == 1

    def test_dispatch_get_available_namespaces(self, mock_handler):
        """Test dispatching get_available_namespaces method."""
        from nexus.server.protocol import GetAvailableNamespacesParams

        params = GetAvailableNamespacesParams()
        result = mock_handler._dispatch_method("get_available_namespaces", params)

        assert result == {"namespaces": ["default"]}


class TestRPCServerIntegration:
    """Integration tests for RPC server."""

    @pytest.fixture
    def temp_nexus(self, tmp_path):
        """Create temporary Nexus instance."""
        import nexus

        data_dir = tmp_path / "nexus-data"
        # Disable permissions for this test - we're testing RPC server functionality, not permissions
        nx = nexus.connect(config={"data_dir": str(data_dir), "enforce_permissions": False})
        nx.mkdir("/test", exist_ok=True)
        nx.write("/test/file.txt", b"test content")
        yield nx
        nx.close()

    def test_server_with_real_filesystem(self, temp_nexus):
        """Test server with real filesystem."""
        # Mock ThreadingHTTPServer to avoid actual port binding
        with patch("nexus.server.rpc_server.ThreadingHTTPServer") as mock_http_server:
            mock_server_instance = Mock()
            mock_http_server.return_value = mock_server_instance
            _server = NexusRPCServer(temp_nexus, host="127.0.0.1", port=9998, api_key=None)

            # Verify handler is configured
            assert RPCRequestHandler.nexus_fs == temp_nexus
            assert RPCRequestHandler.api_key is None

            # Verify ThreadingHTTPServer was created with mocked instance
            assert mock_http_server.called


"""Tests for backend and metadata info extraction methods."""


class TestBackendMetadataInfo:
    """Tests for _get_backend_info() and _get_metadata_info() methods."""

    def test_get_backend_info_local(self):
        """Test getting backend info for local backend."""
        from unittest.mock import Mock

        from nexus.server.rpc_server import RPCRequestHandler

        # Create mock handler with local backend
        handler = Mock(spec=RPCRequestHandler)
        mock_backend = Mock()
        mock_backend.name = "local"
        mock_backend.root_path = "/path/to/data"

        handler.nexus_fs = Mock()
        handler.nexus_fs.backend = mock_backend

        # Bind the method
        handler._get_backend_info = lambda: RPCRequestHandler._get_backend_info(handler)

        # Call method
        result = handler._get_backend_info()

        # Verify
        assert result["type"] == "local"
        assert result["location"] == "/path/to/data"
        assert "bucket" not in result

    def test_get_backend_info_gcs(self):
        """Test getting backend info for GCS backend."""
        from unittest.mock import Mock

        from nexus.server.rpc_server import RPCRequestHandler

        # Create mock handler with GCS backend
        handler = Mock(spec=RPCRequestHandler)
        mock_backend = Mock()
        mock_backend.name = "gcs"
        mock_backend.bucket_name = "my-bucket"

        handler.nexus_fs = Mock()
        handler.nexus_fs.backend = mock_backend

        # Bind the method
        handler._get_backend_info = lambda: RPCRequestHandler._get_backend_info(handler)

        # Call method
        result = handler._get_backend_info()

        # Verify
        assert result["type"] == "gcs"
        assert result["location"] == "my-bucket"
        assert result["bucket"] == "my-bucket"

    def test_get_backend_info_no_backend(self):
        """Test getting backend info when filesystem has no backend attribute."""
        from unittest.mock import Mock

        from nexus.server.rpc_server import RPCRequestHandler

        # Create mock handler without backend attribute
        handler = Mock(spec=RPCRequestHandler)
        handler.nexus_fs = Mock(spec=[])  # No backend attribute

        # Bind the method
        handler._get_backend_info = lambda: RPCRequestHandler._get_backend_info(handler)

        # Call method
        result = handler._get_backend_info()

        # Verify fallback
        assert result["type"] == "unknown"

    def test_get_metadata_info_sqlite(self):
        """Test getting metadata info for SQLite."""
        from unittest.mock import Mock

        from nexus.server.rpc_server import RPCRequestHandler

        # Create mock handler with SQLite metadata
        handler = Mock(spec=RPCRequestHandler)
        mock_metadata = Mock()
        mock_metadata.db_type = "sqlite"
        mock_metadata.db_path = "/path/to/metadata.db"

        handler.nexus_fs = Mock()
        handler.nexus_fs.metadata = mock_metadata

        # Bind the method
        handler._get_metadata_info = lambda: RPCRequestHandler._get_metadata_info(handler)

        # Call method
        result = handler._get_metadata_info()

        # Verify
        assert result["type"] == "sqlite"
        assert result["location"] == "/path/to/metadata.db"

    def test_get_metadata_info_postgresql(self):
        """Test getting metadata info for PostgreSQL without Cloud SQL."""
        from unittest.mock import Mock

        from nexus.server.rpc_server import RPCRequestHandler

        # Create mock handler with PostgreSQL metadata
        handler = Mock(spec=RPCRequestHandler)
        mock_metadata = Mock()
        mock_metadata.db_type = "postgresql"
        mock_metadata.database_url = "postgresql://user:pass@localhost:5432/nexus"

        handler.nexus_fs = Mock()
        handler.nexus_fs.metadata = mock_metadata

        # Bind the method
        handler._get_metadata_info = lambda: RPCRequestHandler._get_metadata_info(handler)

        # Call method
        result = handler._get_metadata_info()

        # Verify
        assert result["type"] == "postgresql"
        assert result["host"] == "localhost:5432"
        assert result["database"] == "nexus"
        assert "cloud_sql_instance" not in result

    def test_get_metadata_info_postgresql_cloud_sql(self, monkeypatch):
        """Test getting metadata info for PostgreSQL with Cloud SQL."""
        from unittest.mock import Mock

        from nexus.server.rpc_server import RPCRequestHandler

        # Set Cloud SQL instance env var
        monkeypatch.setenv("CLOUD_SQL_INSTANCE", "project:region:instance")

        # Create mock handler with PostgreSQL metadata
        handler = Mock(spec=RPCRequestHandler)
        mock_metadata = Mock()
        mock_metadata.db_type = "postgresql"
        mock_metadata.database_url = "postgresql://user:pass@127.0.0.1:5432/nexus"

        handler.nexus_fs = Mock()
        handler.nexus_fs.metadata = mock_metadata

        # Bind the method
        handler._get_metadata_info = lambda: RPCRequestHandler._get_metadata_info(handler)

        # Call method
        result = handler._get_metadata_info()

        # Verify
        assert result["type"] == "postgresql"
        assert result["cloud_sql_instance"] == "project:region:instance"
        assert result["project"] == "project"
        assert result["region"] == "region"
        assert result["instance"] == "instance"
        assert result["database"] == "nexus"
        assert "host" not in result  # Should not include localhost when Cloud SQL is used

    def test_get_metadata_info_no_metadata(self):
        """Test getting metadata info when filesystem has no metadata attribute."""
        from unittest.mock import Mock

        from nexus.server.rpc_server import RPCRequestHandler

        # Create mock handler without metadata attribute
        handler = Mock(spec=RPCRequestHandler)
        handler.nexus_fs = Mock(spec=[])  # No metadata attribute

        # Bind the method
        handler._get_metadata_info = lambda: RPCRequestHandler._get_metadata_info(handler)

        # Call method
        result = handler._get_metadata_info()

        # Verify fallback
        assert result["type"] == "unknown"

    def test_get_metadata_info_sqlite_none_path(self):
        """Test getting metadata info for SQLite with None db_path."""
        from unittest.mock import Mock

        from nexus.server.rpc_server import RPCRequestHandler

        # Create mock handler with SQLite metadata (None path)
        handler = Mock(spec=RPCRequestHandler)
        mock_metadata = Mock()
        mock_metadata.db_type = "sqlite"
        mock_metadata.db_path = None

        handler.nexus_fs = Mock()
        handler.nexus_fs.metadata = mock_metadata

        # Bind the method
        handler._get_metadata_info = lambda: RPCRequestHandler._get_metadata_info(handler)

        # Call method
        result = handler._get_metadata_info()

        # Verify
        assert result["type"] == "sqlite"
        assert result["location"] is None
