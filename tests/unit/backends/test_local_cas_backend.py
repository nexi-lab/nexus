"""Unit tests for CASLocalBackend — full-featured local CAS backend.

Tests cover:
- Basic CRUD (inherited from CASBackend)
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
        data = backend.read_content(r.content_hash)
        assert data == b"hello world"

    def test_deduplication(self, backend):
        r1 = backend.write_content(b"dedup")
        r2 = backend.write_content(b"dedup")
        assert r1.content_hash == r2.content_hash
        assert backend.get_ref_count(r1.content_hash) == 2

    def test_delete_content(self, backend):
        r = backend.write_content(b"delete me")
        backend.delete_content(r.content_hash)
        assert not backend.content_exists(r.content_hash)

    def test_content_exists(self, backend):
        r = backend.write_content(b"exists")
        assert backend.content_exists(r.content_hash)
        assert not backend.content_exists("deadbeef" * 8)

    def test_get_content_size(self, backend):
        r = backend.write_content(b"12345")
        assert backend.get_content_size(r.content_hash) == 5

    def test_stream_content(self, backend):
        r = backend.write_content(b"stream me")
        chunks = list(backend.stream_content(r.content_hash, chunk_size=3))
        assert b"".join(chunks) == b"stream me"

    def test_write_stream(self, backend):
        r = backend.write_stream(iter([b"hel", b"lo"]))
        assert backend.read_content(r.content_hash) == b"hello"

    def test_batch_read(self, backend):
        r1 = backend.write_content(b"one")
        r2 = backend.write_content(b"two")
        result = backend.batch_read_content([r1.content_hash, r2.content_hash])
        assert result[r1.content_hash] == b"one"
        assert result[r2.content_hash] == b"two"


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
        data = backend.read_content(r.content_hash)
        assert data == content

    def test_chunked_content_is_detected(self, backend):
        backend._cdc.threshold = 1024
        content = b"x" * 1200
        r = backend.write_content(content)
        assert backend._cdc.is_chunked(r.content_hash)

    def test_chunked_delete(self, backend):
        backend._cdc.threshold = 1024
        content = b"z" * 1200
        r = backend.write_content(content)
        backend.delete_content(r.content_hash)
        assert not backend.content_exists(r.content_hash)

    def test_small_file_not_chunked(self, backend):
        """Files below threshold should use single-blob storage."""
        small = b"small file"
        r = backend.write_content(small)
        assert not backend._cdc.is_chunked(r.content_hash)

    def test_chunked_content_size(self, backend):
        backend._cdc.threshold = 1024
        content = b"s" * 1200
        r = backend.write_content(content)
        assert backend.get_content_size(r.content_hash) == 1200


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
        hashes = {r.content_hash for r in results}
        assert len(hashes) == 1
        assert backend.get_ref_count(results[0].content_hash) == 50

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
        assert len({r.content_hash for r in results}) == 50


# === Bloom Filter ===


class TestBloomFilter:
    def test_bloom_populated_from_disk(self, tmp_path):
        """Write content, create new backend → Bloom should be populated from disk scan."""
        b1 = CASLocalBackend(root_path=tmp_path)
        r = b1.write_content(b"bloom test")

        # Create new backend from same root — Bloom should be populated from disk
        b2 = CASLocalBackend(root_path=tmp_path)
        assert b2.content_exists(r.content_hash)

    def test_bloom_fast_miss(self, backend):
        assert not backend.content_exists("deadbeef" * 8)


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


class TestProperties:
    def test_name(self, backend):
        assert backend.name == "local"

    def test_has_root_path(self, backend):
        assert backend.has_root_path is True

    def test_supports_parallel_mmap(self, backend):
        assert backend.supports_parallel_mmap_read is True

    def test_supports_multipart(self, backend):
        assert backend.supports_multipart is True
