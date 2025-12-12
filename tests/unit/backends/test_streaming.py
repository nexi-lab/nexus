"""Unit tests for streaming support in backends (Issue #516, #480)."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus.backends.backend import Backend
from nexus.backends.base_blob_connector import BaseBlobStorageConnector
from nexus.backends.local import LocalBackend
from nexus.core.hash_fast import create_hasher, hash_content


class TestBackendWriteStreamDefault:
    """Test default write_stream implementation in Backend base class."""

    def test_default_write_stream_collects_chunks(self) -> None:
        """Test that default implementation collects chunks and calls write_content."""

        class TestBackend(Backend):
            """Minimal test backend."""

            def __init__(self) -> None:
                self.written_content: bytes | None = None

            @property
            def name(self) -> str:
                return "test"

            @property
            def user_scoped(self) -> bool:
                return False

            def write_content(self, content: bytes, context=None) -> str:
                self.written_content = content
                return hash_content(content)

            def read_content(self, content_hash: str, context=None) -> bytes:
                return b""

            def delete_content(self, content_hash: str, context=None) -> None:
                pass

            def content_exists(self, content_hash: str, context=None) -> bool:
                return False

            def get_content_size(self, content_hash: str, context=None) -> int:
                return 0

            def get_ref_count(self, content_hash: str, context=None) -> int:
                return 0

            def mkdir(
                self, path: str, parents: bool = False, exist_ok: bool = False, context=None
            ) -> None:
                pass

            def rmdir(self, path: str, recursive: bool = False, context=None) -> None:
                pass

            def is_directory(self, path: str, context=None) -> bool:
                return False

        backend = TestBackend()

        def chunks():
            yield b"Hello "
            yield b"World"
            yield b"!"

        result_hash = backend.write_stream(chunks())

        assert backend.written_content == b"Hello World!"
        assert result_hash == hash_content(b"Hello World!")


class TestLocalBackendStreaming:
    """Test LocalBackend streaming methods."""

    @pytest.fixture
    def local_backend(self, tmp_path: Path) -> LocalBackend:
        """Create a LocalBackend for testing."""
        return LocalBackend(root_path=tmp_path)

    def test_stream_content_yields_chunks(self, local_backend: LocalBackend) -> None:
        """Test that stream_content yields file content in chunks."""
        # Write some content first
        content = b"A" * 1000 + b"B" * 1000 + b"C" * 1000
        content_hash = local_backend.write_content(content)

        # Stream with small chunks
        chunks = list(local_backend.stream_content(content_hash, chunk_size=500))

        # Should have multiple chunks
        assert len(chunks) == 6  # 3000 bytes / 500 = 6 chunks
        assert b"".join(chunks) == content

    def test_stream_content_default_chunk_size(self, local_backend: LocalBackend) -> None:
        """Test stream_content with default chunk size."""
        content = b"test content"
        content_hash = local_backend.write_content(content)

        chunks = list(local_backend.stream_content(content_hash))

        assert b"".join(chunks) == content

    def test_stream_content_not_found(self, local_backend: LocalBackend) -> None:
        """Test stream_content raises error for missing content."""
        from nexus.core.exceptions import NexusFileNotFoundError

        with pytest.raises(NexusFileNotFoundError):
            list(local_backend.stream_content("nonexistent_hash"))

    def test_write_stream_basic(self, local_backend: LocalBackend) -> None:
        """Test basic write_stream functionality."""

        def chunks():
            yield b"Hello "
            yield b"World!"

        content_hash = local_backend.write_stream(chunks())

        # Verify content was written correctly
        content = local_backend.read_content(content_hash)
        assert content == b"Hello World!"

    def test_write_stream_hash_matches_write_content(self, local_backend: LocalBackend) -> None:
        """Test that write_stream produces same hash as write_content."""
        content = b"Test content for hash comparison"

        # Write using write_content
        hash1 = local_backend.write_content(content)

        # Write using write_stream
        def chunks():
            yield content

        hash2 = local_backend.write_stream(chunks())

        assert hash1 == hash2

    def test_write_stream_increments_ref_count(self, local_backend: LocalBackend) -> None:
        """Test that write_stream increments ref_count for existing content."""
        content = b"Duplicate content"

        # First write
        hash1 = local_backend.write_content(content)
        ref1 = local_backend.get_ref_count(hash1)

        # Second write via stream
        def chunks():
            yield content

        hash2 = local_backend.write_stream(chunks())
        ref2 = local_backend.get_ref_count(hash2)

        assert hash1 == hash2
        assert ref2 == ref1 + 1

    def test_write_stream_large_content(self, local_backend: LocalBackend) -> None:
        """Test write_stream with larger content split into many chunks."""
        chunk_size = 1024
        num_chunks = 100
        content_per_chunk = b"X" * chunk_size

        def chunks():
            for _ in range(num_chunks):
                yield content_per_chunk

        content_hash = local_backend.write_stream(chunks())

        # Verify content
        content = local_backend.read_content(content_hash)
        assert len(content) == chunk_size * num_chunks
        assert content == content_per_chunk * num_chunks

    def test_write_stream_empty_chunks(self, local_backend: LocalBackend) -> None:
        """Test write_stream with empty iterator."""

        def chunks():
            return
            yield  # Make it a generator

        content_hash = local_backend.write_stream(chunks())

        # Should write empty content
        content = local_backend.read_content(content_hash)
        assert content == b""


class TestCreateHasher:
    """Test create_hasher utility function."""

    def test_create_hasher_returns_hasher(self) -> None:
        """Test that create_hasher returns a valid hasher object."""
        hasher = create_hasher()

        # Should have update and hexdigest methods
        assert hasattr(hasher, "update")
        assert hasattr(hasher, "hexdigest")

    def test_create_hasher_produces_consistent_hash(self) -> None:
        """Test that create_hasher produces consistent hashes."""
        content = b"test content"

        hasher1 = create_hasher()
        hasher1.update(content)
        hash1 = hasher1.hexdigest()

        hasher2 = create_hasher()
        hasher2.update(content)
        hash2 = hasher2.hexdigest()

        assert hash1 == hash2

    def test_create_hasher_incremental(self) -> None:
        """Test incremental hashing with create_hasher."""
        hasher = create_hasher()
        hasher.update(b"Hello ")
        hasher.update(b"World!")
        incremental_hash = hasher.hexdigest()

        hasher2 = create_hasher()
        hasher2.update(b"Hello World!")
        full_hash = hasher2.hexdigest()

        assert incremental_hash == full_hash


class TestBaseBlobConnectorStreamContent:
    """Test stream_content in BaseBlobStorageConnector (Issue #480)."""

    def test_stream_content_default_yields_chunks(self) -> None:
        """Test default stream_content yields chunks from _stream_blob."""

        class TestConnector(BaseBlobStorageConnector):
            """Minimal test connector."""

            def __init__(self) -> None:
                super().__init__(bucket_name="test-bucket", prefix="")

            @property
            def name(self) -> str:
                return "test_connector"

            def _upload_blob(self, blob_path, content, content_type):
                return "test-version"

            def _download_blob(self, blob_path, version_id=None):
                # Return test content
                return b"Hello World!", "v1"

            def _delete_blob(self, blob_path):
                pass

            def _blob_exists(self, blob_path):
                return True

            def _get_blob_size(self, blob_path):
                return 12

            def _list_blobs(self, prefix, delimiter="/"):
                return [], []

            def _create_directory_marker(self, blob_path):
                pass

            def _copy_blob(self, source_path, dest_path):
                pass

        connector = TestConnector()

        # Create mock context with backend_path
        context = MagicMock()
        context.backend_path = "test/file.txt"

        chunks = list(connector.stream_content("hash", chunk_size=5, context=context))

        # Should yield chunks of size 5
        assert b"".join(chunks) == b"Hello World!"
        assert len(chunks) == 3  # "Hello" + " Worl" + "d!"

    def test_stream_content_requires_backend_path(self) -> None:
        """Test stream_content raises ValueError without backend_path."""

        class TestConnector(BaseBlobStorageConnector):
            @property
            def name(self) -> str:
                return "test"

            def __init__(self) -> None:
                super().__init__(bucket_name="test", prefix="")

            def _upload_blob(self, blob_path, content, content_type):
                return ""

            def _download_blob(self, blob_path, version_id=None):
                return b"", None

            def _delete_blob(self, blob_path):
                pass

            def _blob_exists(self, blob_path):
                return False

            def _get_blob_size(self, blob_path):
                return 0

            def _list_blobs(self, prefix, delimiter="/"):
                return [], []

            def _create_directory_marker(self, blob_path):
                pass

            def _copy_blob(self, source_path, dest_path):
                pass

        connector = TestConnector()

        with pytest.raises(ValueError, match="requires backend_path"):
            list(connector.stream_content("hash", context=None))

    def test_stream_content_custom_stream_blob(self) -> None:
        """Test that subclass can override _stream_blob for true streaming."""

        class StreamingConnector(BaseBlobStorageConnector):
            """Connector with custom _stream_blob implementation."""

            def __init__(self) -> None:
                super().__init__(bucket_name="test", prefix="")
                self.stream_blob_called = False

            @property
            def name(self) -> str:
                return "streaming_test"

            def _upload_blob(self, blob_path, content, content_type):
                return ""

            def _download_blob(self, blob_path, version_id=None):
                return b"should not be called", None

            def _stream_blob(self, blob_path, chunk_size=8192, version_id=None):
                """Custom streaming implementation."""
                self.stream_blob_called = True
                yield b"chunk1"
                yield b"chunk2"
                yield b"chunk3"

            def _delete_blob(self, blob_path):
                pass

            def _blob_exists(self, blob_path):
                return True

            def _get_blob_size(self, blob_path):
                return 18

            def _list_blobs(self, prefix, delimiter="/"):
                return [], []

            def _create_directory_marker(self, blob_path):
                pass

            def _copy_blob(self, source_path, dest_path):
                pass

        connector = StreamingConnector()
        context = MagicMock()
        context.backend_path = "test/file.txt"

        chunks = list(connector.stream_content("hash", context=context))

        assert connector.stream_blob_called
        assert chunks == [b"chunk1", b"chunk2", b"chunk3"]


class TestReadRangeRPC:
    """Test read_range RPC endpoint (Issue #480)."""

    def test_read_range_basic(self, tmp_path: Path) -> None:
        """Test basic read_range functionality."""
        from nexus.backends.local import LocalBackend
        from nexus.core.nexus_fs import NexusFS

        data_dir = tmp_path / "data"
        db_path = tmp_path / "metadata.db"
        nx = NexusFS(
            backend=LocalBackend(data_dir),
            db_path=db_path,
            auto_parse=False,
            enforce_permissions=False,
        )

        try:
            # Write a test file
            content = b"0123456789ABCDEF"
            nx.write("/test.txt", content)

            # Read ranges
            assert nx.read_range("/test.txt", 0, 5) == b"01234"
            assert nx.read_range("/test.txt", 5, 10) == b"56789"
            assert nx.read_range("/test.txt", 10, 16) == b"ABCDEF"
        finally:
            nx.close()

    def test_read_range_validates_parameters(self, tmp_path: Path) -> None:
        """Test read_range validates start/end parameters."""
        from nexus.backends.local import LocalBackend
        from nexus.core.nexus_fs import NexusFS

        data_dir = tmp_path / "data"
        db_path = tmp_path / "metadata.db"
        nx = NexusFS(
            backend=LocalBackend(data_dir),
            db_path=db_path,
            auto_parse=False,
            enforce_permissions=False,
        )

        try:
            nx.write("/test.txt", b"test content")

            # Negative start should raise
            with pytest.raises(ValueError, match="non-negative"):
                nx.read_range("/test.txt", -1, 5)

            # end < start should raise
            with pytest.raises(ValueError, match="end.*must be >= start"):
                nx.read_range("/test.txt", 10, 5)
        finally:
            nx.close()

    def test_read_range_empty_range(self, tmp_path: Path) -> None:
        """Test read_range with empty range (start == end)."""
        from nexus.backends.local import LocalBackend
        from nexus.core.nexus_fs import NexusFS

        data_dir = tmp_path / "data"
        db_path = tmp_path / "metadata.db"
        nx = NexusFS(
            backend=LocalBackend(data_dir),
            db_path=db_path,
            auto_parse=False,
            enforce_permissions=False,
        )

        try:
            nx.write("/test.txt", b"test content")

            # Empty range should return empty bytes
            assert nx.read_range("/test.txt", 5, 5) == b""
        finally:
            nx.close()

    def test_read_range_beyond_file_size(self, tmp_path: Path) -> None:
        """Test read_range when range extends beyond file size."""
        from nexus.backends.local import LocalBackend
        from nexus.core.nexus_fs import NexusFS

        data_dir = tmp_path / "data"
        db_path = tmp_path / "metadata.db"
        nx = NexusFS(
            backend=LocalBackend(data_dir),
            db_path=db_path,
            auto_parse=False,
            enforce_permissions=False,
        )

        try:
            content = b"short"
            nx.write("/test.txt", content)

            # Range beyond file size should return available content
            result = nx.read_range("/test.txt", 0, 100)
            assert result == content
        finally:
            nx.close()


class TestStatRPC:
    """Test stat() RPC endpoint (Issue #480)."""

    def test_stat_returns_metadata_without_content(self, tmp_path: Path) -> None:
        """Test stat() returns file metadata without reading file content."""
        from nexus.backends.local import LocalBackend
        from nexus.core.nexus_fs import NexusFS

        data_dir = tmp_path / "data"
        db_path = tmp_path / "metadata.db"
        nx = NexusFS(
            backend=LocalBackend(data_dir),
            db_path=db_path,
            auto_parse=False,
            enforce_permissions=False,
        )

        try:
            # Write a test file
            content = b"Hello, World!"
            nx.write("/test.txt", content)

            # stat() should return metadata
            info = nx.stat("/test.txt")

            assert info["size"] == len(content)
            assert info["etag"] is not None
            assert info["version"] is not None
            assert info["is_directory"] is False
        finally:
            nx.close()

    def test_stat_file_not_found(self, tmp_path: Path) -> None:
        """Test stat() raises error for non-existent file."""
        from nexus.backends.local import LocalBackend
        from nexus.core.exceptions import NexusFileNotFoundError
        from nexus.core.nexus_fs import NexusFS

        data_dir = tmp_path / "data"
        db_path = tmp_path / "metadata.db"
        nx = NexusFS(
            backend=LocalBackend(data_dir),
            db_path=db_path,
            auto_parse=False,
            enforce_permissions=False,
        )

        try:
            with pytest.raises(NexusFileNotFoundError):
                nx.stat("/nonexistent.txt")
        finally:
            nx.close()

    def test_stat_directory(self, tmp_path: Path) -> None:
        """Test stat() on a directory."""
        from nexus.backends.local import LocalBackend
        from nexus.core.nexus_fs import NexusFS

        data_dir = tmp_path / "data"
        db_path = tmp_path / "metadata.db"
        nx = NexusFS(
            backend=LocalBackend(data_dir),
            db_path=db_path,
            auto_parse=False,
            enforce_permissions=False,
        )

        try:
            # Create a file in a subdirectory to make an implicit directory
            nx.write("/subdir/file.txt", b"content")

            # stat() on the directory should work
            info = nx.stat("/subdir")

            assert info["is_directory"] is True
            assert info["size"] == 0
        finally:
            nx.close()
