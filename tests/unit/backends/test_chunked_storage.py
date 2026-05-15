"""Tests for Content-Defined Chunking (CDC) storage (Issue #1074)."""

import json
import os
from pathlib import Path

import pytest

from nexus.backends.engines.cdc import (
    CDC_AVG_CHUNK_SIZE,
    CDC_THRESHOLD_BYTES,
    ChunkedReference,
    ChunkInfo,
)
from nexus.backends.storage.cas_local import CASLocalBackend


def _hash_to_path(backend, content_id: str):
    """Helper to construct CAS path from hash for CASLocalBackend."""
    return backend.cas_root / content_id[:2] / content_id[2:4] / content_id


def _write_metadata(backend, content_id: str, metadata: dict):
    """Helper to write metadata for CASLocalBackend."""
    import json

    meta_key = backend._meta_key(content_id)
    meta_bytes = json.dumps(metadata).encode()
    backend._transport.store(meta_key, meta_bytes)


class TestChunkInfo:
    """Tests for ChunkInfo dataclass."""

    def test_to_dict(self) -> None:
        """Test serialization to dict."""
        chunk = ChunkInfo(chunk_hash="abc123", offset=0, length=1024)
        d = chunk.to_dict()
        assert d == {"chunk_hash": "abc123", "offset": 0, "length": 1024}

    def test_from_dict(self) -> None:
        """Test deserialization from dict."""
        d = {"chunk_hash": "def456", "offset": 1024, "length": 2048}
        chunk = ChunkInfo.from_dict(d)
        assert chunk.chunk_hash == "def456"
        assert chunk.offset == 1024
        assert chunk.length == 2048


class TestChunkedReference:
    """Tests for ChunkedReference dataclass."""

    def test_to_dict(self) -> None:
        """Test serialization to dict."""
        ref = ChunkedReference(
            total_size=50 * 1024 * 1024,
            chunk_count=50,
            avg_chunk_size=1024 * 1024,
            content_id="abc123def456",
            chunks=[
                ChunkInfo(chunk_hash="chunk1", offset=0, length=1024000),
                ChunkInfo(chunk_hash="chunk2", offset=1024000, length=1048576),
            ],
        )
        d = ref.to_dict()
        assert d["type"] == "chunked_manifest_v1"
        assert d["total_size"] == 50 * 1024 * 1024
        assert d["chunk_count"] == 50
        assert len(d["chunks"]) == 2

    def test_from_dict(self) -> None:
        """Test deserialization from dict."""
        d = {
            "type": "chunked_manifest_v1",
            "total_size": 10000000,
            "chunk_count": 10,
            "avg_chunk_size": 1000000,
            "content_id": "xyz789",
            "chunks": [
                {"chunk_hash": "c1", "offset": 0, "length": 1000000},
                {"chunk_hash": "c2", "offset": 1000000, "length": 1000000},
            ],
        }
        ref = ChunkedReference.from_dict(d)
        assert ref.total_size == 10000000
        assert ref.chunk_count == 10
        assert len(ref.chunks) == 2
        assert ref.chunks[0].chunk_hash == "c1"

    def test_to_json_roundtrip(self) -> None:
        """Test JSON serialization roundtrip."""
        ref = ChunkedReference(
            total_size=100000,
            chunk_count=5,
            avg_chunk_size=20000,
            content_id="hash123",
            chunks=[
                ChunkInfo(chunk_hash=f"c{i}", offset=i * 20000, length=20000) for i in range(5)
            ],
        )
        json_bytes = ref.to_json()
        ref2 = ChunkedReference.from_json(json_bytes)
        assert ref2.total_size == ref.total_size
        assert ref2.chunk_count == ref.chunk_count
        assert len(ref2.chunks) == len(ref.chunks)

    def test_is_chunked_manifest_true(self) -> None:
        """Test detection of chunked manifest."""
        manifest_json = json.dumps({"type": "chunked_manifest_v1", "chunks": []}).encode()
        assert ChunkedReference.is_chunked_manifest(manifest_json) is True

    def test_is_chunked_manifest_false_large_content(self) -> None:
        """Test that large content is not detected as manifest."""
        large_content = b"x" * (600 * 1024)  # 600KB
        assert ChunkedReference.is_chunked_manifest(large_content) is False

    def test_is_chunked_manifest_false_not_json(self) -> None:
        """Test that non-JSON content is not detected as manifest."""
        binary_content = b"\x00\x01\x02\x03"
        assert ChunkedReference.is_chunked_manifest(binary_content) is False

    def test_is_chunked_manifest_false_wrong_type(self) -> None:
        """Test that JSON without correct type is not detected as manifest."""
        wrong_type = json.dumps({"type": "something_else"}).encode()
        assert ChunkedReference.is_chunked_manifest(wrong_type) is False


class TestChunkedStorageMixin:
    """Tests for ChunkedStorageMixin via CASLocalBackend."""

    @pytest.fixture
    def backend(self, tmp_path: Path) -> CASLocalBackend:
        """Create a CASLocalBackend for testing."""
        return CASLocalBackend(root_path=tmp_path / "backend")

    def test_should_chunk_below_threshold(self, backend: CASLocalBackend) -> None:
        """Test that small content is not chunked."""
        small_content = b"x" * (CDC_THRESHOLD_BYTES - 1)
        assert backend._cdc.should_chunk(small_content) is False

    def test_should_chunk_at_threshold(self, backend: CASLocalBackend) -> None:
        """Test that content at threshold is chunked."""
        threshold_content = b"x" * CDC_THRESHOLD_BYTES
        assert backend._cdc.should_chunk(threshold_content) is True

    def test_should_chunk_above_threshold(self, backend: CASLocalBackend) -> None:
        """Test that large content is chunked."""
        large_content = b"x" * (CDC_THRESHOLD_BYTES + 1)
        assert backend._cdc.should_chunk(large_content) is True

    def test_chunk_content_fixed_fallback(self, backend: CASLocalBackend) -> None:
        """Test fixed-size chunking fallback."""
        content = b"x" * (3 * CDC_AVG_CHUNK_SIZE)
        chunks = backend._cdc._chunk_fixed(content)

        # Should produce 3 chunks
        assert len(chunks) == 3

        # Verify offsets and lengths
        total_length = 0
        for i, (offset, length, chunk_bytes) in enumerate(chunks):
            assert offset == i * CDC_AVG_CHUNK_SIZE
            assert len(chunk_bytes) == length
            total_length += length

        assert total_length == len(content)


class TestCASLocalBackendChunkedWriteRead:
    """Integration tests for chunked write/read operations."""

    @pytest.fixture
    def backend(self, tmp_path: Path) -> CASLocalBackend:
        """Create a CASLocalBackend for testing."""
        return CASLocalBackend(root_path=tmp_path / "backend")

    def test_small_file_not_chunked(self, backend: CASLocalBackend) -> None:
        """Test that small files use single-blob storage."""
        small_content = b"This is a small file that should not be chunked."
        content_id = backend.write_content(small_content).content_id

        # Verify not chunked
        assert not backend._cdc.is_chunked(content_id)

        # Read back
        assert backend.read_content(content_id) == small_content

    def test_large_file_chunked_write_read(self, backend: CASLocalBackend) -> None:
        """Test that large files are chunked and can be read back."""
        # Create content larger than threshold
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 1024 * 1024)  # ~17MB

        # Write
        content_id = backend.write_content(large_content).content_id

        # Verify chunked
        assert backend._cdc.is_chunked(content_id)

        # Read back
        assert backend.read_content(content_id) == large_content

    def test_large_file_chunks_exist(self, backend: CASLocalBackend) -> None:
        """Test that individual chunks are created in CAS."""
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 1024 * 1024)

        content_id = backend.write_content(large_content).content_id

        # Read manifest
        manifest_path = _hash_to_path(backend, content_id)
        manifest = ChunkedReference.from_json(manifest_path.read_bytes())

        # Verify each chunk exists
        for chunk_info in manifest.chunks:
            chunk_path = _hash_to_path(backend, chunk_info.chunk_hash)
            assert chunk_path.exists(), f"Chunk {chunk_info.chunk_hash} should exist"
            assert chunk_path.stat().st_size == chunk_info.length

    def test_chunked_deduplication(self, backend: CASLocalBackend) -> None:
        """Test that identical chunks are deduplicated."""
        # Create two large files with identical prefix
        prefix = os.urandom(CDC_THRESHOLD_BYTES)  # Same prefix
        suffix1 = os.urandom(1024 * 1024)  # Different suffix
        suffix2 = os.urandom(1024 * 1024)  # Different suffix

        content1 = prefix + suffix1
        content2 = prefix + suffix2

        # Write both
        hash1 = backend.write_content(content1).content_id
        hash2 = backend.write_content(content2).content_id

        # Read manifests
        manifest1 = ChunkedReference.from_json(_hash_to_path(backend, hash1).read_bytes())
        manifest2 = ChunkedReference.from_json(_hash_to_path(backend, hash2).read_bytes())

        # Some chunks should be shared (due to identical prefix)
        chunks1 = {c.chunk_hash for c in manifest1.chunks}
        chunks2 = {c.chunk_hash for c in manifest2.chunks}
        shared_chunks = chunks1 & chunks2

        # With CDC, similar content should share some chunks
        # (may not be perfect due to CDC boundaries, but should have some sharing)
        assert len(shared_chunks) > 0, "Similar files should share some chunks"

    def test_chunked_delete_unreferences_chunks(self, backend: CASLocalBackend) -> None:
        """Test that deleting chunked content unreferences chunks."""
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 1024 * 1024)

        # Write
        content_id = backend.write_content(large_content).content_id

        # Get chunk hashes
        manifest = ChunkedReference.from_json(_hash_to_path(backend, content_id).read_bytes())
        chunk_hashes = [c.chunk_hash for c in manifest.chunks]

        # Verify chunks exist
        for ch in chunk_hashes:
            assert _hash_to_path(backend, ch).exists()

        # Delete
        backend.delete_content(content_id)

        # Manifest should be deleted
        assert not _hash_to_path(backend, content_id).exists()

        # Chunks should be deleted
        for ch in chunk_hashes:
            assert not _hash_to_path(backend, ch).exists()

    def test_chunked_dedup_same_hash(self, backend: CASLocalBackend) -> None:
        """Test that writing identical chunked content produces same hash."""
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 1024 * 1024)

        # Write twice (same content = same chunks)
        hash1 = backend.write_content(large_content).content_id
        hash2 = backend.write_content(large_content).content_id

        # Both should have same manifest hash (same content)
        assert hash1 == hash2

    def test_get_content_size_chunked(self, backend: CASLocalBackend) -> None:
        """Test that get_content_size returns original size for chunked content."""
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 500_000)
        original_size = len(large_content)

        content_id = backend.write_content(large_content).content_id

        # Get size
        assert backend.get_content_size(content_id) == original_size

    def test_content_exists_chunked(self, backend: CASLocalBackend) -> None:
        """Test that content_exists works for chunked content."""
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 100_000)

        content_id = backend.write_content(large_content).content_id

        # Should exist
        assert backend.content_exists(content_id) is True

        # Delete
        backend.delete_content(content_id)

        # Should not exist
        assert backend.content_exists(content_id) is False


class TestBackwardCompatibility:
    """Tests for backward compatibility with existing single-blob storage."""

    @pytest.fixture
    def backend(self, tmp_path: Path) -> CASLocalBackend:
        """Create a CASLocalBackend for testing."""
        return CASLocalBackend(root_path=tmp_path / "backend")

    def test_read_existing_single_blob(self, backend: CASLocalBackend) -> None:
        """Test that new code can read existing single-blob files."""
        # Manually create a single-blob file (simulating old storage)
        content = b"This is old single-blob content"
        from nexus.core.hash_fast import hash_content

        content_id = hash_content(content)
        content_path = _hash_to_path(backend, content_id)
        content_path.parent.mkdir(parents=True, exist_ok=True)
        content_path.write_bytes(content)
        _write_metadata(backend, content_id, {"size": len(content)})

        # Should NOT be detected as chunked
        assert not backend._cdc.is_chunked(content_id)

        # Should be readable
        assert backend.read_content(content_id) == content

    def test_mixed_storage_operations(self, backend: CASLocalBackend) -> None:
        """Test that mixed chunked and single-blob operations work together."""
        small_content = b"Small file"
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 100_000)

        # Write both
        small_hash = backend.write_content(small_content).content_id
        large_hash = backend.write_content(large_content).content_id

        # Verify types
        assert not backend._cdc.is_chunked(small_hash)
        assert backend._cdc.is_chunked(large_hash)

        # Read both
        assert backend.read_content(small_hash) == small_content
        assert backend.read_content(large_hash) == large_content

        # Delete both
        backend.delete_content(small_hash)
        backend.delete_content(large_hash)

        # Verify deleted
        assert not backend.content_exists(small_hash)
        assert not backend.content_exists(large_hash)


class TestPerChunkVerification:
    """Tests for per-chunk hash verification during read."""

    @pytest.fixture
    def backend(self, tmp_path: Path) -> CASLocalBackend:
        return CASLocalBackend(root_path=tmp_path / "backend")

    def test_read_chunked_verifies_each_chunk(self, backend: CASLocalBackend) -> None:
        """Test that read_chunked verifies per-chunk hashes."""
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 1024 * 1024)
        content_id = backend.write_content(large_content).content_id

        # Read should succeed with intact chunks
        assert backend.read_content(content_id) == large_content

    def test_corrupted_chunk_raises_value_error(self, backend: CASLocalBackend) -> None:
        """Test that corrupted chunk data raises ValueError."""
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 1024 * 1024)
        content_id = backend.write_content(large_content).content_id

        # Corrupt a chunk on disk
        manifest = ChunkedReference.from_json(_hash_to_path(backend, content_id).read_bytes())
        first_chunk = manifest.chunks[0]
        chunk_path = _hash_to_path(backend, first_chunk.chunk_hash)
        chunk_path.write_bytes(b"CORRUPTED DATA")

        with pytest.raises(ValueError, match="Chunk hash mismatch"):
            backend.read_content(content_id)


class TestRangeReads:
    """Tests for read_content_range and read_chunked_range."""

    @pytest.fixture
    def backend(self, tmp_path: Path) -> CASLocalBackend:
        return CASLocalBackend(root_path=tmp_path / "backend")

    def test_range_read_small_file(self, backend: CASLocalBackend) -> None:
        """Test range read on a non-chunked file."""
        content = b"Hello, World! This is a range read test."
        content_id = backend.write_content(content).content_id

        result = backend.read_content_range(content_id, 0, 5)
        assert result == b"Hello"

        result = backend.read_content_range(content_id, 7, 12)
        assert result == b"World"

    def test_range_read_full_file(self, backend: CASLocalBackend) -> None:
        """Test range read for full file returns same content."""
        content = b"Full file range read"
        content_id = backend.write_content(content).content_id

        result = backend.read_content_range(content_id, 0, len(content))
        assert result == content

    def test_range_read_empty_range(self, backend: CASLocalBackend) -> None:
        """Test range read with start == end returns empty."""
        content = b"empty range"
        content_id = backend.write_content(content).content_id

        result = backend.read_content_range(content_id, 5, 5)
        assert result == b""

    def test_range_read_chunked_file(self, backend: CASLocalBackend) -> None:
        """Test range read on a chunked file."""
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 1024 * 1024)
        content_id = backend.write_content(large_content).content_id

        # Read a range from the middle
        start = CDC_THRESHOLD_BYTES // 2
        end = start + 4096
        result = backend.read_content_range(content_id, start, end)
        assert result == large_content[start:end]

    def test_range_read_chunked_first_bytes(self, backend: CASLocalBackend) -> None:
        """Test reading first few bytes of chunked content."""
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 1024 * 1024)
        content_id = backend.write_content(large_content).content_id

        result = backend.read_content_range(content_id, 0, 100)
        assert result == large_content[:100]

    def test_range_read_chunked_last_bytes(self, backend: CASLocalBackend) -> None:
        """Test reading last few bytes of chunked content."""
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 1024 * 1024)
        content_id = backend.write_content(large_content).content_id

        end = len(large_content)
        start = end - 100
        result = backend.read_content_range(content_id, start, end)
        assert result == large_content[start:end]


class TestCDCChunking:
    """Tests specifically for CDC chunking behavior."""

    @pytest.fixture
    def backend(self, tmp_path: Path) -> CASLocalBackend:
        """Create a CASLocalBackend for testing."""
        return CASLocalBackend(root_path=tmp_path / "backend")

    def test_cdc_produces_variable_chunks(self, backend: CASLocalBackend) -> None:
        """Test that CDC produces variable-sized chunks (not fixed)."""
        # Create content with some patterns that CDC should detect
        large_content = os.urandom(CDC_THRESHOLD_BYTES * 2)

        content_id = backend.write_content(large_content).content_id

        manifest = ChunkedReference.from_json(_hash_to_path(backend, content_id).read_bytes())

        # Get chunk sizes
        chunk_sizes = [c.length for c in manifest.chunks]

        # With CDC, sizes should vary (not all identical)
        # Note: With random content, CDC should find natural boundaries
        # Should have multiple chunks
        assert len(chunk_sizes) > 1

        # Chunks should be within bounds
        for size in chunk_sizes:
            assert (
                size >= backend._cdc.min_chunk or size == chunk_sizes[-1]
            )  # Last chunk can be smaller
            assert size <= backend._cdc.max_chunk

    def test_cdc_chunk_offsets_contiguous(self, backend: CASLocalBackend) -> None:
        """Test that CDC chunk offsets are contiguous (no gaps/overlaps)."""
        large_content = os.urandom(CDC_THRESHOLD_BYTES + 1024 * 1024)

        content_id = backend.write_content(large_content).content_id

        manifest = ChunkedReference.from_json(_hash_to_path(backend, content_id).read_bytes())

        # Verify offsets are contiguous
        expected_offset = 0
        for chunk in manifest.chunks:
            assert chunk.offset == expected_offset, (
                f"Expected offset {expected_offset}, got {chunk.offset}"
            )
            expected_offset += chunk.length

        # Final offset should equal total size
        assert expected_offset == manifest.total_size
