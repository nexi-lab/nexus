"""Unit tests for BackendIOService — content I/O operations.

Tests cover:
- parse_content: asyncio.run() usage, ImportError handling
- batch_read_from_backend: blob path, custom bulk, sequential fallback
- read_content_from_backend: direct blob, fallback to read_content

Part of: #1628 (Split CacheConnectorMixin into focused units)
"""

from unittest.mock import MagicMock, patch

from nexus.backends.misc.backend_io import BackendIOService


class MockSimpleConnector:
    """Connector with no blob support."""

    def __init__(self):
        self.files = {}  # path -> content

    def _read_content_from_backend(self, path, context=None):
        return self.files.get(path)


class MockBlobConnector:
    """Connector with blob storage support."""

    def __init__(self):
        self.blobs = {}  # blob_path -> content

    def _get_blob_path(self, path):
        return f"bucket/{path}"

    def _download_blob(self, blob_path):
        if blob_path in self.blobs:
            return self.blobs[blob_path]
        raise FileNotFoundError(f"No blob: {blob_path}")

    def _bulk_download_blobs(self, blob_paths, version_ids=None):
        return {bp: self.blobs[bp] for bp in blob_paths if bp in self.blobs}

    def _read_content_from_backend(self, path, context=None):
        blob_path = self._get_blob_path(path)
        return self.blobs.get(blob_path)


class TestParseContent:
    def test_returns_none_when_parser_not_available(self):
        connector = MockSimpleConnector()
        svc = BackendIOService(connector)

        with patch.dict("sys.modules", {"nexus.bricks.parsers.markitdown_parser": None}):
            result = svc.parse_content("/test/file.pdf", b"content")

        # ImportError should be caught gracefully
        assert result == (None, None, None)

    def test_returns_none_for_unsupported_format(self):
        connector = MockSimpleConnector()
        svc = BackendIOService(connector)

        # .xyz is not a supported format
        result = svc.parse_content("/test/file.xyz", b"content")
        assert result == (None, None, None)

    def test_returns_none_for_no_extension(self):
        connector = MockSimpleConnector()
        svc = BackendIOService(connector)
        result = svc.parse_content("/test/Makefile", b"content")
        assert result == (None, None, None)


class TestReadContentFromBackend:
    def test_blob_download(self):
        connector = MockBlobConnector()
        connector.blobs["bucket/file.txt"] = b"blob content"
        svc = BackendIOService(connector)

        result = svc.read_content_from_backend("file.txt")
        assert result == b"blob content"

    def test_blob_download_failure_fallback_to_read_content(self):
        connector = MockBlobConnector()
        # No blob stored — will fail
        connector.read_content = MagicMock(return_value=b"fallback")
        svc = BackendIOService(connector)

        result = svc.read_content_from_backend("missing.txt")
        assert result == b"fallback"

    def test_no_blob_no_read_content_returns_none(self):
        connector = MockSimpleConnector()
        svc = BackendIOService(connector)
        result = svc.read_content_from_backend("missing.txt")
        assert result is None


class TestBatchReadFromBackend:
    def test_bulk_download_for_blob_connector(self):
        connector = MockBlobConnector()
        connector.blobs = {
            "bucket/a.txt": b"aaa",
            "bucket/b.txt": b"bbb",
        }
        svc = BackendIOService(connector)

        results = svc.batch_read_from_backend(["a.txt", "b.txt"])
        assert results["a.txt"] == b"aaa"
        assert results["b.txt"] == b"bbb"

    def test_custom_bulk_download(self):
        connector = MagicMock()
        # Remove blob attributes to trigger custom bulk path
        del connector._bulk_download_blobs
        del connector._get_blob_path
        connector._bulk_download_contents.return_value = {"a.txt": b"aaa"}
        svc = BackendIOService(connector)

        results = svc.batch_read_from_backend(["a.txt"])
        assert results == {"a.txt": b"aaa"}

    def test_sequential_fallback(self):
        connector = MockSimpleConnector()
        connector.files = {"a.txt": b"aaa", "b.txt": b"bbb"}
        svc = BackendIOService(connector)

        results = svc.batch_read_from_backend(["a.txt", "b.txt", "missing.txt"])
        assert len(results) == 2
        assert results["a.txt"] == b"aaa"
        assert results["b.txt"] == b"bbb"

    def test_partial_blob_failure(self):
        connector = MockBlobConnector()
        connector.blobs = {"bucket/a.txt": b"aaa"}  # b.txt missing
        svc = BackendIOService(connector)

        results = svc.batch_read_from_backend(["a.txt", "b.txt"])
        assert len(results) == 1
        assert results["a.txt"] == b"aaa"


class TestGenerateEmbeddings:
    def test_delegates_to_connector(self):
        connector = MagicMock()
        svc = BackendIOService(connector)
        svc.generate_embeddings("/test/file.txt")
        connector._generate_embeddings.assert_called_once_with("/test/file.txt")

    def test_no_op_when_connector_lacks_method(self):
        connector = MockSimpleConnector()  # No _generate_embeddings
        svc = BackendIOService(connector)
        # Should not raise
        svc.generate_embeddings("/test/file.txt")
