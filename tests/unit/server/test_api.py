"""Unit tests for S3-compatible API server."""

import hashlib
import io
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from unittest.mock import Mock, patch

from nexus import NexusFilesystem
from nexus.core.exceptions import NexusFileNotFoundError
from nexus.server.api import APIRequestHandler, NexusHTTPServer
from nexus.server.auth import SigV4Validator, create_simple_credentials_store


class TestAPIRequestHandler:
    """Test API request handler."""

    def setup_method(self):
        """Set up test fixtures."""
        # Create mock filesystem with metadata attribute
        self.mock_nx = Mock(spec=NexusFilesystem)
        self.mock_nx.metadata = Mock()

        # Create auth validator
        self.credentials_store = create_simple_credentials_store("testkey", "testsecret")
        self.auth_validator = SigV4Validator(self.credentials_store)

        # Set class attributes
        APIRequestHandler.nexus_fs = self.mock_nx
        APIRequestHandler.auth_validator = self.auth_validator
        APIRequestHandler.bucket_name = "nexus"

    def create_handler(self, method, path, headers=None, body=b""):
        """Create a mock request handler."""
        if headers is None:
            headers = {}

        # Create mock socket and request
        mock_socket = Mock()
        mock_socket.makefile.return_value = io.BytesIO(body)

        mock_request = Mock()
        mock_request.makefile.return_value = io.BytesIO(body)

        # Create handler
        handler = APIRequestHandler(mock_request, ("127.0.0.1", 12345), Mock())
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        handler.command = method
        handler.path = path
        handler.request_version = "HTTP/1.1"

        # Set headers
        handler.headers = {}
        for key, value in headers.items():
            handler.headers[key] = value

        return handler

    def test_log_message(self):
        """Test log message override."""
        handler = self.create_handler("GET", "/nexus")

        # Should not raise exception
        handler.log_message("Test %s", "message")

    @patch.object(APIRequestHandler, "_validate_auth")
    @patch.object(APIRequestHandler, "_handle_list_objects")
    def test_do_get_list_objects(self, mock_list, mock_auth):
        """Test GET request for listing objects."""
        mock_auth.return_value = (True, None)

        handler = self.create_handler(
            "GET",
            "/nexus?list-type=2",
            headers={"host": "localhost:8080"},
        )

        handler.do_GET()

        mock_list.assert_called_once()

    @patch.object(APIRequestHandler, "_validate_auth")
    @patch.object(APIRequestHandler, "_handle_get_object")
    def test_do_get_object(self, mock_get, mock_auth):
        """Test GET request for getting an object."""
        mock_auth.return_value = (True, None)

        handler = self.create_handler(
            "GET",
            "/nexus/test.txt",
            headers={"host": "localhost:8080"},
        )

        handler.do_GET()

        mock_get.assert_called_once_with("test.txt")

    @patch.object(APIRequestHandler, "_validate_auth")
    @patch.object(APIRequestHandler, "_send_error_response")
    def test_do_get_auth_failure(self, mock_error, mock_auth):
        """Test GET request with auth failure."""
        mock_auth.return_value = (False, "Invalid signature")

        handler = self.create_handler(
            "GET",
            "/nexus",
            headers={"host": "localhost:8080"},
        )

        handler.do_GET()

        mock_error.assert_called_once_with(403, "AccessDenied", "Invalid signature")

    @patch.object(APIRequestHandler, "_validate_auth")
    @patch.object(APIRequestHandler, "_handle_head_object")
    def test_do_head_object(self, mock_head, mock_auth):
        """Test HEAD request for object."""
        mock_auth.return_value = (True, None)

        handler = self.create_handler(
            "HEAD",
            "/nexus/test.txt",
            headers={"host": "localhost:8080"},
        )

        handler.do_HEAD()

        mock_head.assert_called_once_with("test.txt")

    @patch.object(APIRequestHandler, "_validate_auth")
    def test_do_head_bucket(self, mock_auth):
        """Test HEAD request for bucket (HeadBucket)."""
        mock_auth.return_value = (True, None)

        handler = self.create_handler(
            "HEAD",
            "/nexus",
            headers={"host": "localhost:8080"},
        )

        handler.send_response = Mock()
        handler.end_headers = Mock()

        handler.do_HEAD()

        handler.send_response.assert_called_once_with(200)

    @patch.object(APIRequestHandler, "_validate_auth")
    @patch.object(APIRequestHandler, "_handle_put_object")
    def test_do_put_object(self, mock_put, mock_auth):
        """Test PUT request for object."""
        mock_auth.return_value = (True, None)

        body = b"test content"
        handler = self.create_handler(
            "PUT",
            "/nexus/test.txt",
            headers={"host": "localhost:8080", "content-length": str(len(body))},
            body=body,
        )

        handler.do_PUT()

        # Verify _validate_auth was called
        mock_auth.assert_called_once()
        # Verify _handle_put_object was called with test.txt
        assert mock_put.call_count == 1
        assert mock_put.call_args[0][0] == "test.txt"  # First positional arg is object_key

    @patch.object(APIRequestHandler, "_validate_auth")
    @patch.object(APIRequestHandler, "_handle_create_bucket")
    def test_do_put_create_bucket(self, mock_create, mock_auth):
        """Test PUT request for creating bucket."""
        mock_auth.return_value = (True, None)

        handler = self.create_handler(
            "PUT",
            "/nexus",
            headers={"host": "localhost:8080", "content-length": "0"},
        )

        handler.do_PUT()

        mock_create.assert_called_once()

    @patch.object(APIRequestHandler, "_validate_auth")
    @patch.object(APIRequestHandler, "_handle_delete_object")
    def test_do_delete_object(self, mock_delete, mock_auth):
        """Test DELETE request for object."""
        mock_auth.return_value = (True, None)

        handler = self.create_handler(
            "DELETE",
            "/nexus/test.txt",
            headers={"host": "localhost:8080"},
        )

        handler.do_DELETE()

        mock_delete.assert_called_once_with("test.txt")

    def test_handle_list_objects(self):
        """Test list objects handler."""
        # Mock filesystem list response
        self.mock_nx.list.return_value = [
            {
                "path": "/file1.txt",
                "size": 100,
                "etag": "abc123",
                "modified_at": datetime(2025, 10, 18, 12, 0, 0, tzinfo=UTC),
            },
            {
                "path": "/file2.txt",
                "size": 200,
                "etag": "def456",
                "modified_at": datetime(2025, 10, 18, 13, 0, 0, tzinfo=UTC),
            },
        ]

        handler = self.create_handler("GET", "/nexus")
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()

        handler._handle_list_objects({"prefix": [""], "max-keys": ["1000"]})

        # Verify response was sent
        handler.send_response.assert_called_once_with(200)
        assert handler.send_header.call_count >= 2

        # Parse XML response from wfile
        xml_response = handler.wfile.getvalue()
        root = ET.fromstring(xml_response)

        # Verify XML structure (handle namespace)
        # The tag includes namespace: {http://s3.amazonaws.com/doc/2006-03-01/}ListBucketResult
        assert root.tag.endswith("ListBucketResult")

        # Use namespace-aware find
        ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
        contents = root.findall("s3:Contents", ns)
        assert len(contents) == 2

    def test_handle_get_object_success(self):
        """Test get object handler success."""
        content = b"test file content"
        self.mock_nx.read.return_value = content

        # Mock metadata
        mock_meta = Mock()
        mock_meta.etag = hashlib.md5(content).hexdigest()
        mock_meta.modified_at = datetime(2025, 10, 18, 12, 0, 0, tzinfo=UTC)

        self.mock_nx.metadata.get.return_value = mock_meta

        handler = self.create_handler("GET", "/nexus/test.txt")
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()

        handler._handle_get_object("test.txt")

        self.mock_nx.read.assert_called_once_with("/test.txt")
        handler.send_response.assert_called_once_with(200)

        # Verify content was written
        response_body = handler.wfile.getvalue()
        assert response_body == content

    def test_handle_get_object_not_found(self):
        """Test get object handler with file not found."""
        self.mock_nx.read.side_effect = NexusFileNotFoundError("/test.txt")

        handler = self.create_handler("GET", "/nexus/test.txt")
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()

        handler._handle_get_object("test.txt")

        handler.send_response.assert_called_once_with(404)

    @patch("nexus.server.api.isinstance")
    def test_handle_head_object_success(self, mock_isinstance):
        """Test head object handler success."""
        self.mock_nx.exists.return_value = True

        # Make isinstance check pass for NexusFS
        mock_isinstance.return_value = True

        # Create a proper mock metadata object
        mock_meta = Mock()
        mock_meta.size = 100
        mock_meta.etag = "abc123"
        mock_meta.mime_type = "text/plain"
        mock_meta.modified_at = datetime(2025, 10, 18, 12, 0, 0, tzinfo=UTC)

        self.mock_nx.metadata.get.return_value = mock_meta

        handler = self.create_handler("HEAD", "/nexus/test.txt")
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()

        handler._handle_head_object("test.txt")

        handler.send_response.assert_called_once_with(200)
        assert handler.send_header.call_count >= 3

    def test_handle_head_object_not_found(self):
        """Test head object handler with file not found."""
        self.mock_nx.exists.return_value = False

        handler = self.create_handler("HEAD", "/nexus/test.txt")
        handler.send_response = Mock()
        handler.end_headers = Mock()

        handler._handle_head_object("test.txt")

        handler.send_response.assert_called_once_with(404)

    def test_handle_create_bucket(self):
        """Test create bucket handler."""
        handler = self.create_handler("PUT", "/nexus")
        handler.send_response = Mock()
        handler.end_headers = Mock()

        handler._handle_create_bucket()

        handler.send_response.assert_called_once_with(200)

    def test_handle_put_object(self):
        """Test put object handler."""
        content = b"test content"
        handler = self.create_handler("PUT", "/nexus/test.txt")
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()

        handler._handle_put_object("test.txt", content)

        self.mock_nx.write.assert_called_once_with("/test.txt", content)
        handler.send_response.assert_called_once_with(200)

    def test_handle_delete_object(self):
        """Test delete object handler."""
        self.mock_nx.exists.return_value = True

        handler = self.create_handler("DELETE", "/nexus/test.txt")
        handler.send_response = Mock()
        handler.end_headers = Mock()

        handler._handle_delete_object("test.txt")

        self.mock_nx.delete.assert_called_once_with("/test.txt")
        handler.send_response.assert_called_once_with(204)

    def test_handle_delete_object_not_exists(self):
        """Test delete object handler when file doesn't exist."""
        self.mock_nx.exists.return_value = False

        handler = self.create_handler("DELETE", "/nexus/test.txt")
        handler.send_response = Mock()
        handler.end_headers = Mock()

        handler._handle_delete_object("test.txt")

        self.mock_nx.delete.assert_not_called()
        handler.send_response.assert_called_once_with(204)

    def test_send_error_response(self):
        """Test sending error response."""
        handler = self.create_handler("GET", "/nexus")
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()

        handler._send_error_response(404, "NoSuchKey", "File not found")

        handler.send_response.assert_called_once_with(404)

        # Parse XML response
        xml_response = handler.wfile.getvalue()
        root = ET.fromstring(xml_response)

        assert root.tag == "Error"
        assert root.find("Code").text == "NoSuchKey"
        assert root.find("Message").text == "File not found"


class TestNexusHTTPServer:
    """Test Nexus HTTP server."""

    def test_server_initialization(self):
        """Test server initialization."""
        mock_nx = Mock(spec=NexusFilesystem)
        credentials_store = create_simple_credentials_store("key", "secret")
        auth_validator = SigV4Validator(credentials_store)

        server = NexusHTTPServer(
            nexus_fs=mock_nx,
            auth_validator=auth_validator,
            host="localhost",
            port=8080,
            bucket_name="testbucket",
        )

        assert server.nexus_fs == mock_nx
        assert server.auth_validator == auth_validator
        assert server.host == "localhost"
        assert server.port == 8080
        assert server.bucket_name == "testbucket"
        assert APIRequestHandler.nexus_fs == mock_nx
        assert APIRequestHandler.auth_validator == auth_validator
        assert APIRequestHandler.bucket_name == "testbucket"

    def test_server_shutdown(self):
        """Test server shutdown."""
        mock_nx = Mock(spec=NexusFilesystem)
        mock_nx.close = Mock()

        credentials_store = create_simple_credentials_store("key", "secret")
        auth_validator = SigV4Validator(credentials_store)

        server = NexusHTTPServer(
            nexus_fs=mock_nx,
            auth_validator=auth_validator,
            host="localhost",
            port=8081,
        )

        # Mock the HTTPServer methods
        server.server.shutdown = Mock()
        server.server.server_close = Mock()

        server.shutdown()

        server.server.shutdown.assert_called_once()
        server.server.server_close.assert_called_once()
        mock_nx.close.assert_called_once()
