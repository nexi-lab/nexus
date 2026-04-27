"""Unit tests for streaming support in backends (Issue #516, #480, #1625)."""

import os
import random
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus.backends.base.backend import Backend
from nexus.backends.base.path_addressing_engine import PathAddressingEngine
from nexus.backends.storage.cas_local import CASLocalBackend
from nexus.core.config import ParseConfig, PermissionConfig
from nexus.core.hash_fast import create_hasher, hash_content
from nexus.core.object_store import WriteResult as ObjectStoreWriteResult
from nexus.factory import create_nexus_fs
from nexus.storage.record_store import SQLAlchemyRecordStore
from tests.helpers.dict_metastore import DictMetastore


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

            def write_content(
                self, content: bytes, content_id: str = "", *, offset: int = 0, context=None
            ) -> ObjectStoreWriteResult:
                self.written_content = content
                return ObjectStoreWriteResult(content_id=hash_content(content), size=len(content))

            def read_content(self, content_hash: str, context=None) -> bytes:
                return b""

            def delete_content(self, content_hash: str, context=None) -> None:
                pass

            def content_exists(self, content_hash: str, context=None) -> bool:
                return False

            def get_content_size(self, content_hash: str, context=None) -> int:
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

        result = backend.write_stream(chunks())
        result_hash = result.content_id

        assert backend.written_content == b"Hello World!"
        assert result_hash == hash_content(b"Hello World!")


class TestCASLocalBackendStreaming:
    """Test CASLocalBackend streaming methods."""

    @pytest.fixture
    def local_backend(self, tmp_path: Path) -> CASLocalBackend:
        """Create a CASLocalBackend for testing."""
        return CASLocalBackend(root_path=tmp_path)

    def test_stream_content_yields_chunks(self, local_backend: CASLocalBackend) -> None:
        """Test that stream_content yields file content in chunks."""
        # Write some content first
        content = b"A" * 1000 + b"B" * 1000 + b"C" * 1000
        content_hash = local_backend.write_content(content).content_id

        # Stream with small chunks
        chunks = list(local_backend.stream_content(content_hash, chunk_size=500))

        # Should have multiple chunks
        assert len(chunks) == 6  # 3000 bytes / 500 = 6 chunks
        assert b"".join(chunks) == content

    def test_stream_content_default_chunk_size(self, local_backend: CASLocalBackend) -> None:
        """Test stream_content with default chunk size."""
        content = b"test content"
        content_hash = local_backend.write_content(content).content_id

        chunks = list(local_backend.stream_content(content_hash))

        assert b"".join(chunks) == content

    def test_stream_content_not_found(self, local_backend: CASLocalBackend) -> None:
        """Test stream_content raises error for missing content."""
        from nexus.contracts.exceptions import NexusFileNotFoundError

        with pytest.raises(NexusFileNotFoundError):
            list(local_backend.stream_content("nonexistent_hash"))

    def test_write_stream_basic(self, local_backend: CASLocalBackend) -> None:
        """Test basic write_stream functionality."""

        def chunks():
            yield b"Hello "
            yield b"World!"

        result = local_backend.write_stream(chunks())
        content_hash = result.content_id

        # Verify content was written correctly
        content = local_backend.read_content(content_hash)
        assert content == b"Hello World!"

    def test_write_stream_hash_matches_write_content(self, local_backend: CASLocalBackend) -> None:
        """Test that write_stream produces same hash as write_content."""
        content = b"Test content for hash comparison"

        # Write using write_content
        hash1 = local_backend.write_content(content).content_id

        # Write using write_stream
        def chunks():
            yield content

        hash2 = local_backend.write_stream(chunks()).content_id

        assert hash1 == hash2

    def test_write_stream_large_content(self, local_backend: CASLocalBackend) -> None:
        """Test write_stream with larger content split into many chunks."""
        chunk_size = 1024
        num_chunks = 100
        content_per_chunk = b"X" * chunk_size

        def chunks():
            for _ in range(num_chunks):
                yield content_per_chunk

        result = local_backend.write_stream(chunks())
        content_hash = result.content_id

        # Verify content
        content = local_backend.read_content(content_hash)
        assert len(content) == chunk_size * num_chunks
        assert content == content_per_chunk * num_chunks

    def test_write_stream_empty_chunks(self, local_backend: CASLocalBackend) -> None:
        """Test write_stream with empty iterator."""

        def chunks():
            return
            yield  # Make it a generator

        result = local_backend.write_stream(chunks())
        content_hash = result.content_id

        # Should write empty content
        content = local_backend.read_content(content_hash)
        assert content == b""

    def test_write_stream_returns_size(self, local_backend: CASLocalBackend) -> None:
        """Test that write_stream returns file size via .size (Issue #1625)."""
        content = b"Size tracking test" * 100

        def chunks():
            yield content

        result = local_backend.write_stream(chunks())
        assert result.size == len(content)


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

    def test_incremental_hash_matches_oneshot(self) -> None:
        """Incremental create_hasher() produces same hash as hash_content() (Issue #1625)."""
        rng = random.Random(42)  # noqa: S311 -- deterministic, not crypto
        for content in [b"", b"x", b"A" * 100_000, os.urandom(1_000_000)]:
            hasher = create_hasher()
            offset = 0
            while offset < len(content):
                chunk_size = min(rng.randint(1, 8192), len(content) - offset)
                hasher.update(content[offset : offset + chunk_size])
                offset += chunk_size
            assert hasher.hexdigest() == hash_content(content)


class TestStreamingMemoryEfficiency:
    """Verify write_stream does NOT buffer entire content in memory (Issue #1625)."""

    def test_write_stream_bounded_memory(self, tmp_path: Path) -> None:
        """Write 10 MB via write_stream; peak memory should stay well under 10 MB.

        Uses 10 MB (below 16 MB CDC threshold) to test the pure streaming
        path without triggering CDC re-chunking, which intentionally reads
        the blob back.
        """
        import tracemalloc

        backend = CASLocalBackend(root_path=tmp_path)

        total_bytes = 10 * 1024 * 1024  # 10 MB (below 16 MB CDC threshold)
        chunk_size = 65536  # 64 KB chunks

        # Pre-create a shared chunk to avoid counting its allocation
        chunk_data = b"\x00" * chunk_size

        def big_chunks():
            remaining = total_bytes
            while remaining > 0:
                size = min(chunk_size, remaining)
                if size == chunk_size:
                    yield chunk_data
                else:
                    yield chunk_data[:size]
                remaining -= size

        # Start tracing AFTER backend init (Bloom filter, etc.)
        tracemalloc.start()
        baseline = tracemalloc.get_traced_memory()[0]

        result = backend.write_stream(big_chunks())
        assert result.content_id is not None

        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Net allocation during write_stream should be far less than the file size.
        # Allow 2 MB for overhead (hasher state, temp file handle, etc.)
        net_peak = peak - baseline
        assert net_peak < 2 * 1024 * 1024, (
            f"Net peak memory {net_peak / 1024 / 1024:.1f} MB exceeds 2 MB bound -- "
            f"write_stream is likely buffering entire content"
        )

        # Verify the content size
        assert result.size == total_bytes


class TestPathAddressingEngineStreamContent:
    """Test stream_content in PathAddressingEngine (Issue #480)."""

    def test_stream_content_default_yields_chunks(self) -> None:
        """Test default stream_content yields chunks from transport.stream."""
        from collections.abc import Iterator

        class TestTransport:
            """Minimal in-memory transport."""

            transport_name = "memory"

            def __init__(self) -> None:
                self.files: dict[str, bytes] = {}

            def store(self, key, data, content_type=""):
                self.files[key] = data
                return None

            def fetch(self, key, version_id=None):
                if key not in self.files:
                    raise FileNotFoundError(key)
                return self.files[key], version_id

            def remove(self, key):
                self.files.pop(key, None)

            def exists(self, key):
                return key in self.files

            def get_size(self, key):
                return len(self.files.get(key, b""))

            def list_keys(self, prefix="", delimiter="/"):
                return [k for k in self.files if k.startswith(prefix)], []

            def copy_key(self, src_key, dst_key):
                if src_key in self.files:
                    self.files[dst_key] = self.files[src_key]

            def create_dir(self, key):
                self.files[key] = b""

            def stream(self, key, chunk_size=8192, version_id=None) -> Iterator[bytes]:
                data, _ = self.fetch(key, version_id)
                for i in range(0, len(data), chunk_size):
                    yield data[i : i + chunk_size]

        transport = TestTransport()
        transport.files["test/file.txt"] = b"Hello World!"
        connector = PathAddressingEngine(
            transport, backend_name="test_connector", bucket_name="test-bucket"
        )

        # Create mock context with backend_path
        context = MagicMock()
        context.backend_path = "test/file.txt"

        chunks = list(connector.stream_content("hash", chunk_size=5, context=context))

        # Should yield chunks of size 5
        assert b"".join(chunks) == b"Hello World!"
        assert len(chunks) == 3  # "Hello" + " Worl" + "d!"

    def test_stream_content_requires_backend_path(self) -> None:
        """Test stream_content raises ValueError without backend_path."""
        from collections.abc import Iterator

        class TestTransport:
            transport_name = "memory"

            def __init__(self) -> None:
                self.files: dict[str, bytes] = {}

            def store(self, key, data, content_type=""):
                return None

            def fetch(self, key, version_id=None):
                return b"", None

            def remove(self, key):
                pass

            def exists(self, key):
                return False

            def get_size(self, key):
                return 0

            def list_keys(self, prefix="", delimiter="/"):
                return [], []

            def copy_key(self, src_key, dst_key):
                pass

            def create_dir(self, key):
                pass

            def stream(self, key, chunk_size=8192, version_id=None) -> Iterator[bytes]:
                return iter([])

        transport = TestTransport()
        connector = PathAddressingEngine(transport, backend_name="test", bucket_name="test")

        with pytest.raises(ValueError, match="requires backend_path"):
            list(connector.stream_content("hash", context=None))

    def test_stream_content_custom_stream(self) -> None:
        """Test that transport can provide custom stream for true streaming."""
        from collections.abc import Iterator

        class StreamingTransport:
            """Transport with custom stream implementation."""

            transport_name = "memory"

            def __init__(self) -> None:
                self.files: dict[str, bytes] = {}
                self.stream_called = False

            def store(self, key, data, content_type=""):
                return None

            def fetch(self, key, version_id=None):
                return b"should not be called", None

            def remove(self, key):
                pass

            def exists(self, key):
                return True

            def get_size(self, key):
                return 18

            def list_keys(self, prefix="", delimiter="/"):
                return [], []

            def copy_key(self, src_key, dst_key):
                pass

            def create_dir(self, key):
                pass

            def stream(self, key, chunk_size=8192, version_id=None) -> Iterator[bytes]:
                """Custom streaming implementation."""
                self.stream_called = True
                yield b"chunk1"
                yield b"chunk2"
                yield b"chunk3"

        transport = StreamingTransport()
        connector = PathAddressingEngine(
            transport, backend_name="streaming_test", bucket_name="test"
        )
        context = MagicMock()
        context.backend_path = "test/file.txt"

        chunks = list(connector.stream_content("hash", context=context))

        assert transport.stream_called
        assert chunks == [b"chunk1", b"chunk2", b"chunk3"]


class TestReadRangeRPC:
    """Test read_range RPC endpoint (Issue #480)."""

    @pytest.mark.asyncio
    async def test_read_range_basic(self, tmp_path: Path) -> None:
        """Test basic read_range functionality."""
        from nexus.backends.storage.cas_local import CASLocalBackend

        data_dir = tmp_path / "data"
        db_path = tmp_path / "metadata.db"
        nx = create_nexus_fs(
            backend=CASLocalBackend(data_dir),
            metadata_store=DictMetastore(),
            record_store=SQLAlchemyRecordStore(db_path=db_path),
            parsing=ParseConfig(auto_parse=False),
            permissions=PermissionConfig(enforce=False),
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

    @pytest.mark.asyncio
    async def test_read_range_validates_parameters(self, tmp_path: Path) -> None:
        """Test read_range validates start/end parameters."""
        from nexus.backends.storage.cas_local import CASLocalBackend

        data_dir = tmp_path / "data"
        db_path = tmp_path / "metadata.db"
        nx = create_nexus_fs(
            backend=CASLocalBackend(data_dir),
            metadata_store=DictMetastore(),
            record_store=SQLAlchemyRecordStore(db_path=db_path),
            parsing=ParseConfig(auto_parse=False),
            permissions=PermissionConfig(enforce=False),
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

    @pytest.mark.asyncio
    async def test_read_range_empty_range(self, tmp_path: Path) -> None:
        """Test read_range with empty range (start == end)."""
        from nexus.backends.storage.cas_local import CASLocalBackend

        data_dir = tmp_path / "data"
        db_path = tmp_path / "metadata.db"
        nx = create_nexus_fs(
            backend=CASLocalBackend(data_dir),
            metadata_store=DictMetastore(),
            record_store=SQLAlchemyRecordStore(db_path=db_path),
            parsing=ParseConfig(auto_parse=False),
            permissions=PermissionConfig(enforce=False),
        )

        try:
            content = b"test content"
            nx.write("/test.txt", content)

            # Empty range should return empty bytes
            assert nx.read_range("/test.txt", 5, 5) == b""
        finally:
            nx.close()

    @pytest.mark.asyncio
    async def test_read_range_beyond_file_size(self, tmp_path: Path) -> None:
        """Test read_range when range extends beyond file size."""
        from nexus.backends.storage.cas_local import CASLocalBackend

        data_dir = tmp_path / "data"
        db_path = tmp_path / "metadata.db"
        nx = create_nexus_fs(
            backend=CASLocalBackend(data_dir),
            metadata_store=DictMetastore(),
            record_store=SQLAlchemyRecordStore(db_path=db_path),
            parsing=ParseConfig(auto_parse=False),
            permissions=PermissionConfig(enforce=False),
        )

        try:
            content = b"short"
            nx.write("/test.txt", content)

            # Range beyond file size should return available content
            result = nx.read_range("/test.txt", 0, len(content) + 100)
            assert result == content
        finally:
            nx.close()


class TestStatRPC:
    """Test stat() RPC endpoint (Issue #480)."""

    @pytest.mark.asyncio
    async def test_stat_returns_metadata_without_content(self, tmp_path: Path) -> None:
        """Test stat() returns file metadata without reading file content."""
        from nexus.backends.storage.cas_local import CASLocalBackend

        data_dir = tmp_path / "data"
        db_path = tmp_path / "metadata.db"
        nx = create_nexus_fs(
            backend=CASLocalBackend(data_dir),
            metadata_store=DictMetastore(),
            record_store=SQLAlchemyRecordStore(db_path=db_path),
            parsing=ParseConfig(auto_parse=False),
            permissions=PermissionConfig(enforce=False),
        )

        try:
            # Write a test file
            content = b"Hello, World!"
            nx.write("/test.txt", content)

            # stat() should return metadata
            info = nx.stat("/test.txt")

            assert info["size"] == len(content)
            assert info["content_id"] is not None
            assert info["version"] is not None
            assert info["is_directory"] is False
        finally:
            nx.close()

    @pytest.mark.asyncio
    async def test_stat_file_not_found(self, tmp_path: Path) -> None:
        """Test stat() raises error for non-existent file."""
        from nexus.backends.storage.cas_local import CASLocalBackend
        from nexus.contracts.exceptions import NexusFileNotFoundError

        data_dir = tmp_path / "data"
        db_path = tmp_path / "metadata.db"
        nx = create_nexus_fs(
            backend=CASLocalBackend(data_dir),
            metadata_store=DictMetastore(),
            record_store=SQLAlchemyRecordStore(db_path=db_path),
            parsing=ParseConfig(auto_parse=False),
            permissions=PermissionConfig(enforce=False),
        )

        try:
            with pytest.raises(NexusFileNotFoundError):
                nx.stat("/nonexistent.txt")
        finally:
            nx.close()

    @pytest.mark.asyncio
    async def test_stat_directory(self, tmp_path: Path) -> None:
        """Test stat() on a directory."""
        from nexus.backends.storage.cas_local import CASLocalBackend

        data_dir = tmp_path / "data"
        db_path = tmp_path / "metadata.db"
        nx = create_nexus_fs(
            backend=CASLocalBackend(data_dir),
            metadata_store=DictMetastore(),
            record_store=SQLAlchemyRecordStore(db_path=db_path),
            parsing=ParseConfig(auto_parse=False),
            permissions=PermissionConfig(enforce=False),
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
