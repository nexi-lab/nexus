"""Integration tests for RPC path unscoping.

Verifies that the RPC handler strips internal zone/tenant/user prefixes
from paths before returning them to API clients.

Related: Issue #1202 - list('/') returns paths with /tenant: prefix
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from nexus.server.rpc_server import RPCRequestHandler


@pytest.fixture
def mock_filesystem():
    """Create mock filesystem returning internal-prefixed paths."""
    fs = Mock()
    fs.is_directory = Mock(return_value=False)
    fs.exists = Mock(return_value=True)
    return fs


@pytest.fixture
def mock_handler(mock_filesystem):
    """Create a mock RPC handler with necessary attributes."""
    handler = Mock(spec=RPCRequestHandler)
    handler.nexus_fs = mock_filesystem
    handler.api_key = None
    handler.auth_provider = None
    handler.event_loop = None
    handler.headers = {}
    # Bind the actual dispatch method
    handler._dispatch_method = lambda method, params: RPCRequestHandler._dispatch_method(
        handler, method, params
    )
    handler._get_operation_context = Mock(return_value=None)
    handler.exposed_methods = {}
    return handler


class TestListPathUnscoping:
    """Test that list() results have internal prefixes stripped."""

    def test_list_strips_tenant_prefix_from_paths(
        self, mock_handler, mock_filesystem
    ) -> None:
        """Issue #1202: list('/') should not return /tenant: prefixed paths."""
        mock_filesystem.list = Mock(
            return_value=[
                "/tenant:default/connector/gcs_demo/auto-test.txt",
                "/tenant:default/connector/gcs_demo/copy1.txt",
                "/tenant:default/user:admin/workspace/file.txt",
            ]
        )

        params = Mock()
        params.path = "/"
        params.recursive = True
        params.details = False
        params.prefix = None
        params.show_parsed = True

        result = mock_handler._dispatch_method("list", params)

        assert result == {
            "files": [
                "/connector/gcs_demo/auto-test.txt",
                "/connector/gcs_demo/copy1.txt",
                "/workspace/file.txt",
            ]
        }

    def test_list_strips_zone_prefix_from_paths(
        self, mock_handler, mock_filesystem
    ) -> None:
        """list() strips /zone/ prefixed paths."""
        mock_filesystem.list = Mock(
            return_value=[
                "/zone/default/user:admin/workspace/file.txt",
                "/zone/default/connector/s3/data.csv",
            ]
        )

        params = Mock()
        params.path = "/"
        params.recursive = True
        params.details = False
        params.prefix = None
        params.show_parsed = True

        result = mock_handler._dispatch_method("list", params)

        assert result == {
            "files": [
                "/workspace/file.txt",
                "/connector/s3/data.csv",
            ]
        }

    def test_list_strips_prefix_from_detail_dicts(
        self, mock_handler, mock_filesystem
    ) -> None:
        """list(details=True) strips prefixes from path keys in dicts."""
        mock_filesystem.list = Mock(
            return_value=[
                {
                    "path": "/tenant:default/user:admin/workspace/file.txt",
                    "size": 100,
                    "etag": "abc123",
                },
            ]
        )

        params = Mock()
        params.path = "/"
        params.recursive = True
        params.details = True
        params.prefix = None
        params.show_parsed = True

        result = mock_handler._dispatch_method("list", params)

        files = result["files"]
        assert len(files) == 1
        assert files[0]["path"] == "/workspace/file.txt"
        assert files[0]["size"] == 100
        assert files[0]["etag"] == "abc123"

    def test_list_preserves_already_clean_paths(
        self, mock_handler, mock_filesystem
    ) -> None:
        """list() doesn't modify paths that don't have internal prefixes."""
        mock_filesystem.list = Mock(
            return_value=["/workspace/file.txt", "/skills/my-skill/main.py"]
        )

        params = Mock()
        params.path = "/"
        params.recursive = True
        params.details = False
        params.prefix = None
        params.show_parsed = True

        result = mock_handler._dispatch_method("list", params)

        assert result == {
            "files": ["/workspace/file.txt", "/skills/my-skill/main.py"]
        }


class TestWritePathUnscoping:
    """Test that write() response has internal prefix stripped."""

    def test_write_strips_tenant_prefix(
        self, mock_handler, mock_filesystem
    ) -> None:
        """write() response path should be unscoped."""
        mock_filesystem.write = Mock(
            return_value={
                "path": "/tenant:default/user:admin/workspace/file.txt",
                "etag": "abc123",
                "version": 1,
                "size": 100,
            }
        )

        params = Mock()
        params.path = "/workspace/file.txt"
        params.content = b"hello"
        params.if_match = None
        params.if_none_match = False
        params.force = False

        result = mock_handler._dispatch_method("write", params)

        assert result["path"] == "/workspace/file.txt"
        assert result["etag"] == "abc123"


class TestReadPathUnscoping:
    """Test that read(return_metadata=True) response has internal prefix stripped."""

    def test_read_metadata_strips_tenant_prefix(
        self, mock_handler, mock_filesystem
    ) -> None:
        """read(return_metadata=True) should strip prefix from response path."""
        mock_filesystem.read = Mock(
            return_value={
                "content": b"hello world",
                "path": "/tenant:default/user:admin/workspace/file.txt",
                "virtual_path": "/tenant:default/user:admin/workspace/file.txt",
                "etag": "abc123",
                "size": 11,
            }
        )

        params = Mock()
        params.path = "/workspace/file.txt"
        params.return_metadata = True

        result = mock_handler._dispatch_method("read", params)

        assert result["path"] == "/workspace/file.txt"
        assert result["virtual_path"] == "/workspace/file.txt"
        assert result["content"] == b"hello world"
        assert result["etag"] == "abc123"


class TestAppendPathUnscoping:
    """Test that append() response has internal prefix stripped."""

    def test_append_strips_zone_prefix(
        self, mock_handler, mock_filesystem
    ) -> None:
        """append() response path should be unscoped."""
        mock_filesystem.append = Mock(
            return_value={
                "path": "/zone/default/user:admin/workspace/log.txt",
                "etag": "def456",
                "version": 3,
                "size": 500,
            }
        )

        params = Mock()
        params.path = "/workspace/log.txt"
        params.content = b"new line\n"
        params.if_match = None
        params.force = False

        result = mock_handler._dispatch_method("append", params)

        assert result["path"] == "/workspace/log.txt"
        assert result["etag"] == "def456"
        assert result["version"] == 3


class TestGetMetadataPathUnscoping:
    """Test that get_metadata() response has internal prefix stripped."""

    def test_get_metadata_strips_tenant_prefix(
        self, mock_handler, mock_filesystem
    ) -> None:
        """get_metadata() should strip internal prefix from metadata path."""
        metadata = Mock()
        metadata.path = "/tenant:default/user:admin/workspace/file.txt"
        metadata.backend_name = "local"
        metadata.physical_path = "/data/file.txt"
        metadata.size = 100
        metadata.etag = "abc123"
        metadata.mime_type = "text/plain"
        metadata.created_at = "2024-01-01T00:00:00Z"
        metadata.modified_at = "2024-01-01T00:00:00Z"
        metadata.version = 1
        metadata.zone_id = "default"

        mock_metadata_store = Mock()
        mock_metadata_store.get = Mock(return_value=metadata)
        mock_filesystem.metadata = mock_metadata_store
        mock_filesystem.is_directory = Mock(return_value=False)

        params = Mock()
        params.path = "/workspace/file.txt"

        result = mock_handler._dispatch_method("get_metadata", params)

        meta = result["metadata"]
        assert meta["path"] == "/workspace/file.txt"
        assert meta["backend_name"] == "local"
        assert meta["size"] == 100


class TestGlobPathUnscoping:
    """Test that glob() results have internal prefixes stripped."""

    def test_glob_strips_prefix(self, mock_handler, mock_filesystem) -> None:
        """glob() should strip internal prefixes from matches."""
        mock_filesystem.glob = Mock(
            return_value=[
                "/tenant:default/user:admin/workspace/test.py",
                "/tenant:default/connector/gcs/data.csv",
            ]
        )

        params = Mock()
        params.pattern = "*.py"
        params.path = "/"

        result = mock_handler._dispatch_method("glob", params)

        assert result == {
            "matches": [
                "/workspace/test.py",
                "/connector/gcs/data.csv",
            ]
        }


class TestGrepPathUnscoping:
    """Test that grep() results have internal prefixes stripped."""

    def test_grep_strips_prefix_from_file_key(
        self, mock_handler, mock_filesystem
    ) -> None:
        """grep() should strip internal prefixes from file/path keys in results."""
        mock_filesystem.grep = Mock(
            return_value=[
                {
                    "file": "/tenant:default/user:admin/workspace/test.py",
                    "path": "/tenant:default/user:admin/workspace/test.py",
                    "line": 10,
                    "content": "import os",
                },
            ]
        )

        params = Mock()
        params.pattern = "import"
        params.path = "/"
        params.file_pattern = None
        params.ignore_case = False
        params.max_results = 100
        params.search_mode = None

        result = mock_handler._dispatch_method("grep", params)

        results = result["results"]
        assert len(results) == 1
        assert results[0]["file"] == "/workspace/test.py"
        assert results[0]["path"] == "/workspace/test.py"
        assert results[0]["line"] == 10
