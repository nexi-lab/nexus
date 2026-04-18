"""CRUD verification: CASAddressingEngine(LocalTransport) end-to-end.

This is the **first time** production CRUD has been verified on the new
CAS x Transport composition with a real filesystem. Previous tests
used InMemoryTransport.

Tests cover:
- write/read roundtrip with hash verification
- Deduplication (same content → same hash)
- content_exists / get_content_size
- stream_content / write_stream / batch_read
- Directory operations (mkdir, rmdir, is_directory, list_dir)
- Feature DI: Bloom filter fast-miss, content cache hit, stripe lock
- Concurrent writes with stripe lock

References:
    - Issue #1323: CAS x Backend orthogonal composition
"""

from unittest.mock import MagicMock

import pytest

from nexus.backends.base.cas_addressing_engine import CASAddressingEngine
from nexus.backends.transports.local_transport import LocalTransport
from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.core.hash_fast import hash_content
from nexus.core.object_store import WriteResult


@pytest.fixture
def transport(tmp_path):
    return LocalTransport(root_path=tmp_path, fsync=False)


@pytest.fixture
def backend(transport):
    return CASAddressingEngine(transport, backend_name="test-local")


@pytest.fixture
def backend_with_features(transport):
    """CASAddressingEngine with Feature DI enabled.

    Bloom filter was removed in R10f — direct `_transport.exists()` is
    fast enough that the pre-filter no longer pays back.
    """
    cache = SimpleCache()
    callback = MagicMock()
    return CASAddressingEngine(
        transport,
        backend_name="test-local-features",
        content_cache=cache,
        on_write_callback=callback,
    )


# === Simple test doubles for Feature DI ===


class SimpleCache:
    """Minimal cache for testing."""

    def __init__(self):
        self._store: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self._store.get(key)

    def put(self, key: str, value: bytes) -> None:
        self._store[key] = value


# === Basic CRUD ===


class TestWriteReadRoundtrip:
    def test_write_returns_write_result(self, backend):
        result = backend.write_content(b"hello")
        assert isinstance(result, WriteResult)
        assert result.content_id == hash_content(b"hello")
        assert result.size == 5

    def test_read_returns_exact_content(self, backend):
        result = backend.write_content(b"hello world")
        data = backend.read_content(result.content_id)
        assert data == b"hello world"

    def test_read_nonexistent_raises(self, backend):
        with pytest.raises(NexusFileNotFoundError):
            backend.read_content("deadbeef" * 8)

    def test_write_empty_content(self, backend):
        result = backend.write_content(b"")
        data = backend.read_content(result.content_id)
        assert data == b""

    def test_write_large_content(self, backend):
        large = b"x" * (1024 * 1024)  # 1MB
        result = backend.write_content(large)
        data = backend.read_content(result.content_id)
        assert data == large


class TestDeduplication:
    def test_same_content_same_hash(self, backend):
        r1 = backend.write_content(b"dedup")
        r2 = backend.write_content(b"dedup")
        assert r1.content_id == r2.content_id


class TestDeleteContent:
    def test_delete_single_ref(self, backend):
        r = backend.write_content(b"del me")
        backend.delete_content(r.content_id)
        assert not backend.content_exists(r.content_id)

    def test_delete_nonexistent_raises(self, backend):
        with pytest.raises(NexusFileNotFoundError):
            backend.delete_content("deadbeef" * 8)


class TestContentExists:
    def test_exists_after_write(self, backend):
        r = backend.write_content(b"exists")
        assert backend.content_exists(r.content_id) is True

    def test_not_exists(self, backend):
        assert backend.content_exists("deadbeef" * 8) is False

    def test_not_exists_after_delete(self, backend):
        r = backend.write_content(b"temp")
        backend.delete_content(r.content_id)
        assert backend.content_exists(r.content_id) is False


class TestGetContentSize:
    def test_size_correct(self, backend):
        r = backend.write_content(b"12345")
        assert backend.get_content_size(r.content_id) == 5

    def test_size_nonexistent_raises(self, backend):
        with pytest.raises(NexusFileNotFoundError):
            backend.get_content_size("deadbeef" * 8)


# === Streaming ===


class TestStreamContent:
    def test_stream_roundtrip(self, backend):
        r = backend.write_content(b"stream me")
        chunks = list(backend.stream_content(r.content_id, chunk_size=3))
        assert b"".join(chunks) == b"stream me"

    def test_stream_nonexistent_raises(self, backend):
        with pytest.raises(NexusFileNotFoundError):
            list(backend.stream_content("deadbeef" * 8))


class TestWriteStream:
    def test_write_stream_roundtrip(self, backend):
        chunks = iter([b"hel", b"lo ", b"wor", b"ld"])
        r = backend.write_stream(chunks)
        assert r.content_id == hash_content(b"hello world")
        data = backend.read_content(r.content_id)
        assert data == b"hello world"


class TestBatchRead:
    def test_batch_read_all_found(self, backend):
        r1 = backend.write_content(b"one")
        r2 = backend.write_content(b"two")
        result = backend.batch_read_content([r1.content_id, r2.content_id])
        assert result[r1.content_id] == b"one"
        assert result[r2.content_id] == b"two"

    def test_batch_read_missing_returns_none(self, backend):
        r = backend.write_content(b"exists")
        result = backend.batch_read_content([r.content_id, "deadbeef" * 8])
        assert result[r.content_id] == b"exists"
        assert result["deadbeef" * 8] is None

    def test_batch_read_empty(self, backend):
        assert backend.batch_read_content([]) == {}


# === Directory Operations ===


class TestDirectoryOperations:
    def test_mkdir_and_is_directory(self, backend):
        backend.mkdir("workspace", parents=True, exist_ok=True)
        assert backend.is_directory("workspace") is True

    def test_mkdir_nested(self, backend):
        backend.mkdir("a/b/c", parents=True, exist_ok=True)
        assert backend.is_directory("a/b/c") is True

    def test_is_directory_false(self, backend):
        assert backend.is_directory("nonexistent") is False

    def test_is_directory_root(self, backend):
        assert backend.is_directory("") is True

    def test_rmdir(self, backend):
        backend.mkdir("temp", parents=True, exist_ok=True)
        backend.rmdir("temp")
        assert backend.is_directory("temp") is False

    def test_rmdir_nonexistent_raises(self, backend):
        with pytest.raises(NexusFileNotFoundError):
            backend.rmdir("nonexistent")

    def test_list_dir(self, backend):
        backend.mkdir("parent", parents=True, exist_ok=True)
        backend.mkdir("parent/child1", parents=True, exist_ok=True)
        backend.mkdir("parent/child2", parents=True, exist_ok=True)
        entries = backend.list_dir("parent")
        assert "child1/" in entries
        assert "child2/" in entries


# === Feature DI: Content Cache ===


class TestContentCache:
    def test_cache_hit_on_second_read(self, backend_with_features):
        b = backend_with_features
        r = b.write_content(b"cached data")
        # First read populates cache (write also populates)
        data1 = b.read_content(r.content_id)
        assert data1 == b"cached data"
        # Cache should have it
        assert b._cache.get(r.content_id) == b"cached data"

    def test_cache_populated_on_write(self, backend_with_features):
        b = backend_with_features
        r = b.write_content(b"write cache")
        assert b._cache.get(r.content_id) == b"write cache"


# === Feature DI: on_write_callback ===


class TestOnWriteCallback:
    def test_callback_called_on_new_write(self, backend_with_features):
        b = backend_with_features
        b.write_content(b"new content")
        b._on_write_callback.assert_called_once()

    def test_callback_not_called_on_dedup(self, backend_with_features):
        b = backend_with_features
        b.write_content(b"dedup content")
        b._on_write_callback.reset_mock()
        b.write_content(b"dedup content")
        b._on_write_callback.assert_not_called()


# === No Feature DI (cloud-style, all None) ===


class TestNoFeatures:
    """Verify CASAddressingEngine works correctly with no features injected (cloud mode)."""

    def test_crud_without_features(self, backend):
        r = backend.write_content(b"cloud-style")
        data = backend.read_content(r.content_id)
        assert data == b"cloud-style"
        backend.delete_content(r.content_id)
        assert not backend.content_exists(r.content_id)
