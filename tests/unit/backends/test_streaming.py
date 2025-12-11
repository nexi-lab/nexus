"""Unit tests for streaming support in backends (Issue #516)."""

from pathlib import Path

import pytest

from nexus.backends.backend import Backend
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
