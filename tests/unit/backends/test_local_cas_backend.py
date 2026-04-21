"""Unit tests for CASLocalBackend — full-featured local CAS backend.

Tests cover:
- Basic CRUD (inherited from CASAddressingEngine)
- CDC: write large file → chunked, read back → reassembled
- Multipart: init/upload_parts/complete/abort
- Concurrent writes with stripe lock (50 threads)
- Bloom filter populated from disk at startup
- Feature DI wiring (cache, callback)

References:
    - Issue #1323: CAS x Backend orthogonal composition
"""

import threading
from unittest.mock import MagicMock

import pytest

from nexus.backends.storage.cas_local import CASLocalBackend
from nexus.contracts.exceptions import BackendError
from nexus.core.object_store import WriteResult


@pytest.fixture
def backend(tmp_path):
    return CASLocalBackend(root_path=tmp_path)


@pytest.fixture
def backend_with_callback(tmp_path):
    callback = MagicMock()
    return CASLocalBackend(root_path=tmp_path, on_write_callback=callback)


# === Basic CRUD ===


class TestBasicCRUD:
    def test_write_read_roundtrip(self, backend):
        r = backend.write_content(b"hello world")
        assert isinstance(r, WriteResult)
        data = backend.read_content(r.content_id)
        assert data == b"hello world"

    def test_deduplication(self, backend):
        r1 = backend.write_content(b"dedup")
        r2 = backend.write_content(b"dedup")
        assert r1.content_id == r2.content_id

    def test_delete_content(self, backend):
        r = backend.write_content(b"delete me")
        backend.delete_content(r.content_id)
        assert not backend.content_exists(r.content_id)

    def test_content_exists(self, backend):
        r = backend.write_content(b"exists")
        assert backend.content_exists(r.content_id)
        assert not backend.content_exists("deadbeef" * 8)

    def test_get_content_size(self, backend):
        r = backend.write_content(b"12345")
        assert backend.get_content_size(r.content_id) == 5

    def test_stream_content(self, backend):
        r = backend.write_content(b"stream me")
        chunks = list(backend.stream_content(r.content_id, chunk_size=3))
        assert b"".join(chunks) == b"stream me"

    def test_write_stream(self, backend):
        r = backend.write_stream(iter([b"hel", b"lo"]))
        assert backend.read_content(r.content_id) == b"hello"

    def test_batch_read(self, backend):
        r1 = backend.write_content(b"one")
        r2 = backend.write_content(b"two")
        result = backend.batch_read_content([r1.content_id, r2.content_id])
        assert result[r1.content_id] == b"one"
        assert result[r2.content_id] == b"two"


# === Directory Operations ===


class TestDirectoryOps:
    def test_mkdir_and_list(self, backend):
        backend.mkdir("workspace", parents=True, exist_ok=True)
        backend.mkdir("workspace/sub", parents=True, exist_ok=True)
        assert backend.is_directory("workspace")
        entries = backend.list_dir("workspace")
        assert "sub/" in entries

    def test_rmdir(self, backend):
        backend.mkdir("temp", parents=True, exist_ok=True)
        backend.rmdir("temp")
        assert not backend.is_directory("temp")


# === CDC Chunked Storage ===


class TestCDCChunkedStorage:
    """CDC tests use low threshold but default CDC chunk sizes.

    FastCDC requires min_chunk <= data_size, so we keep default 256KB
    min_chunk and use data above 1KB threshold. With default settings,
    the content becomes a single chunk — that's fine, we're testing
    the manifest path, not chunk boundary detection.
    """

    def test_large_file_chunked_roundtrip(self, backend):
        """Write file above threshold → manifest → read back → verify."""
        backend._cdc.threshold = 1024  # 1KB threshold for testing

        content = b"A" * 500 + b"B" * 500 + b"C" * 200  # 1200 bytes
        r = backend.write_content(content)
        data = backend.read_content(r.content_id)
        assert data == content

    def test_chunked_content_is_detected(self, backend):
        backend._cdc.threshold = 1024
        content = b"x" * 1200
        r = backend.write_content(content)
        assert backend._cdc.is_chunked(r.content_id)

    def test_chunked_delete(self, backend):
        backend._cdc.threshold = 1024
        content = b"z" * 1200
        r = backend.write_content(content)
        backend.delete_content(r.content_id)
        assert not backend.content_exists(r.content_id)

    def test_small_file_not_chunked(self, backend):
        """Files below threshold should use single-blob storage."""
        small = b"small file"
        r = backend.write_content(small)
        assert not backend._cdc.is_chunked(r.content_id)

    def test_chunked_content_size(self, backend):
        backend._cdc.threshold = 1024
        content = b"s" * 1200
        r = backend.write_content(content)
        assert backend.get_content_size(r.content_id) == 1200


# === Multipart Upload ===


class TestMultipartUpload:
    def test_multipart_roundtrip(self, backend):
        upload_id = backend.init_multipart("test/file.bin")

        parts = []
        for i in range(3):
            part = backend.upload_part("test/file.bin", upload_id, i + 1, f"part{i}".encode())
            parts.append(part)

        content_hash = backend.complete_multipart("test/file.bin", upload_id, parts)
        data = backend.read_content(content_hash)
        assert data == b"part0part1part2"

    def test_multipart_abort(self, backend):
        upload_id = backend.init_multipart("test/file.bin")
        backend.upload_part("test/file.bin", upload_id, 1, b"data")
        backend.abort_multipart("test/file.bin", upload_id)
        # Upload dir should be cleaned up
        upload_dir = backend.root_path / "uploads" / upload_id
        assert not upload_dir.exists()

    def test_multipart_nonexistent_upload_raises(self, backend):
        with pytest.raises(BackendError):
            backend.upload_part("test/file.bin", "nonexistent", 1, b"data")

    def test_multipart_complete_nonexistent_raises(self, backend):
        with pytest.raises(BackendError):
            backend.complete_multipart("test/file.bin", "nonexistent", [])


# === Concurrent Writes ===


class TestConcurrentWrites:
    def test_50_threads_same_content(self, backend):
        content = b"concurrent-content"
        results = []
        errors = []

        def _write():
            try:
                results.append(backend.write_content(content))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_write) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(results) == 50
        hashes = {r.content_id for r in results}
        assert len(hashes) == 1
        assert backend.content_exists(results[0].content_id)

    def test_50_threads_different_content(self, backend):
        results = []
        errors = []

        def _write(i):
            try:
                results.append(backend.write_content(f"content-{i}".encode()))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_write, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(results) == 50
        assert len({r.content_id for r in results}) == 50


# === content_exists smoke (Bloom filter was dropped in R10f) ===


class TestContentExists:
    def test_existing_content_survives_new_backend_instance(self, tmp_path):
        """Write content, open a fresh backend handle on the same root →
        the hash is still findable (disk is the source of truth)."""
        b1 = CASLocalBackend(root_path=tmp_path)
        r = b1.write_content(b"content survives")

        b2 = CASLocalBackend(root_path=tmp_path)
        assert b2.content_exists(r.content_id)

    def test_missing_content_reports_false(self, backend):
        assert not backend.content_exists("deadbeef" * 8)


# === Incremental Chunk Write (Dedup) ===


class TestIncrementalChunkWrite:
    """Tests for CAS+CDC incremental write optimization (#1763).

    Uses low threshold + small fixed chunks for fast testing.
    """

    @pytest.fixture
    def cdc_backend(self, tmp_path):
        b = CASLocalBackend(root_path=tmp_path)
        b._cdc.threshold = 1024  # 1KB threshold
        b._cdc.min_chunk = 256
        b._cdc.avg_chunk = 512
        b._cdc.max_chunk = 1024
        return b

    def test_incremental_write_skips_existing_chunks(self, cdc_backend):
        """Second write of identical content should skip store for all chunks."""
        content = b"A" * 2048  # Above threshold, will be chunked

        cdc_backend.write_content(content)

        # Patch store to count non-meta blob writes on second write
        original_store = cdc_backend._transport.store
        blob_writes = []

        def counting_store(key, data, *args, **kwargs):
            if not key.endswith(".meta"):
                blob_writes.append(key)
            return original_store(key, data, *args, **kwargs)

        cdc_backend._transport.store = counting_store

        # Second write — chunks already exist, should be deduped
        cdc_backend.write_content(content)

        # Only manifest blob should be written (chunk blobs skipped via dedup)
        assert len(blob_writes) == 1, (
            f"Expected 1 blob write (manifest only), got {len(blob_writes)}: {blob_writes}"
        )

    def test_incremental_write_partial_overlap(self, cdc_backend):
        """Two files sharing some chunks → shared chunks exist in CAS."""
        # Use fixed chunking (avg_chunk=512) so we get predictable boundaries
        # File A: [AAAA][BBBB][CCCC][DDDD] (4 chunks of 512)
        chunk_a = b"A" * 512
        chunk_b = b"B" * 512
        chunk_c = b"C" * 512
        chunk_d = b"D" * 512
        chunk_e = b"E" * 512

        content_a = chunk_a + chunk_b + chunk_c + chunk_d  # 2048 bytes
        content_b = chunk_a + chunk_b + chunk_e + chunk_d  # shares chunks a, b, d

        r_a = cdc_backend.write_content(content_a)
        r_b = cdc_backend.write_content(content_b)

        # Both should be readable
        assert cdc_backend.read_content(r_a.content_id) == content_a
        assert cdc_backend.read_content(r_b.content_id) == content_b

    def test_delete_after_incremental_write(self, cdc_backend):
        """Write then delete — blob removed."""
        content = bytes(range(256)) * 8  # 2048 bytes, varied

        r = cdc_backend.write_content(content)
        assert cdc_backend.content_exists(r.content_id)

        # Delete — blob gone
        cdc_backend.delete_content(r.content_id)
        assert not cdc_backend.content_exists(r.content_id)


# === On-Write Callback ===


class TestOnWriteCallback:
    def test_callback_on_new_write(self, backend_with_callback):
        b = backend_with_callback
        b.write_content(b"new")
        b._on_write_callback.assert_called_once()

    def test_callback_not_on_dedup(self, backend_with_callback):
        b = backend_with_callback
        b.write_content(b"dedup")
        b._on_write_callback.reset_mock()
        b.write_content(b"dedup")
        b._on_write_callback.assert_not_called()


# === Properties ===


class TestOffsetWrite:
    """Offset write (POSIX pwrite semantics) for CAS single-blob files."""

    def test_offset_write_splice(self, backend):
        """Write at offset splices into existing content."""
        r1 = backend.write_content(b"Hello World")
        r2 = backend.write_content(b"Earth", r1.content_id, offset=6)
        result = backend.read_content(r2.content_id)
        assert result == b"Hello Earth"

    def test_offset_write_beyond_eof_zero_fills(self, backend):
        """Offset beyond EOF zero-fills the gap."""
        r1 = backend.write_content(b"ABC")
        r2 = backend.write_content(b"XY", r1.content_id, offset=5)
        result = backend.read_content(r2.content_id)
        assert result == b"ABC\x00\x00XY"

    def test_offset_zero_unchanged(self, backend):
        """offset=0 (default) behaves as whole-file replace — backward compat."""
        backend.write_content(b"original")
        r2 = backend.write_content(b"replaced", offset=0)
        result = backend.read_content(r2.content_id)
        assert result == b"replaced"

    def test_offset_write_extends_file(self, backend):
        """Writing past the end extends the file."""
        r1 = backend.write_content(b"Hello")
        r2 = backend.write_content(b" World!", r1.content_id, offset=5)
        result = backend.read_content(r2.content_id)
        assert result == b"Hello World!"
        assert r2.size == 12


class TestOffsetWriteCDC:
    """Offset write for CAS+CDC chunked files."""

    @pytest.fixture
    def cdc_backend(self, tmp_path):
        b = CASLocalBackend(root_path=tmp_path)
        b._cdc.threshold = 1024  # 1KB threshold
        b._cdc.min_chunk = 256
        b._cdc.avg_chunk = 512
        b._cdc.max_chunk = 1024
        return b

    def test_partial_write_in_one_chunk(self, cdc_backend):
        """Partial write affecting a single chunk."""
        # Write a chunked file: 2KB of 'A's
        content = b"A" * 2048
        r1 = cdc_backend.write_content(content)
        assert cdc_backend._cdc.is_chunked(r1.content_id)

        # Overwrite 10 bytes at offset 100 (within first chunk)
        r2 = cdc_backend.write_content(b"BBBBBBBBBB", r1.content_id, offset=100)

        # Read back and verify splice
        result = cdc_backend.read_content(r2.content_id)
        assert len(result) == 2048
        assert result[100:110] == b"BBBBBBBBBB"
        assert result[:100] == b"A" * 100
        assert result[110:] == b"A" * (2048 - 110)

    def test_partial_write_spanning_chunks(self, cdc_backend):
        """Partial write that spans across chunk boundaries."""
        # Write a chunked file: first half 'A', second half 'B'
        content = b"A" * 1024 + b"B" * 1024
        r1 = cdc_backend.write_content(content)

        # Write across the boundary (around offset 1020)
        patch = b"X" * 20
        r2 = cdc_backend.write_content(patch, r1.content_id, offset=1014)

        result = cdc_backend.read_content(r2.content_id)
        assert len(result) == 2048
        assert result[1014:1034] == patch
        assert result[:1014] == b"A" * 1014
        assert result[1034:] == b"B" * (2048 - 1034)

    def test_unaffected_chunks_reused(self, cdc_backend):
        """Chunks not touched by the partial write should have same hash (reused)."""
        from nexus.backends.engines.cdc import ChunkedReference

        # Use fixed-size chunks for predictable boundaries
        content = b"A" * 512 + b"B" * 512 + b"C" * 512 + b"D" * 512
        r1 = cdc_backend.write_content(content)

        # Get chunk hashes from original manifest
        m1_data = cdc_backend._transport.fetch(cdc_backend._blob_key(r1.content_id))[0]
        m1 = ChunkedReference.from_json(m1_data)

        # Write 10 bytes at offset 10 (within first chunk region only)
        r2 = cdc_backend.write_content(b"Z" * 10, r1.content_id, offset=10)

        m2_data = cdc_backend._transport.fetch(cdc_backend._blob_key(r2.content_id))[0]
        m2 = ChunkedReference.from_json(m2_data)

        # Suffix chunks (after the affected region) should have the same hashes
        # The last chunks should be identical since we only modified the beginning
        suffix_hashes_m1 = {ci.chunk_hash for ci in m1.chunks if ci.offset >= 512}
        suffix_hashes_m2 = {ci.chunk_hash for ci in m2.chunks if ci.offset >= 512}
        assert suffix_hashes_m1 == suffix_hashes_m2, "Unaffected suffix chunks should be reused"


class TestProperties:
    def test_name(self, backend):
        assert backend.name == "local"

    def test_has_root_path(self, backend):
        assert backend.has_root_path is True

    def test_supports_multipart(self, backend):
        assert backend.supports_multipart is True
