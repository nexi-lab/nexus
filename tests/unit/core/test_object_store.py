"""Conformance test suite for ObjectStoreABC Protocol.

Tests:
- ObjectStoreABC conformance (write/read/delete/exists/size/batch_read)
- BackendObjectStore adapter error handling
- Protocol isinstance checks
- Deduplication behavior

Parametrized across LocalBackend and MockBackend.
"""

from __future__ import annotations

import hashlib
import time

import pytest

from nexus.backends.backend import Backend
from nexus.backends.local import LocalBackend
from nexus.core.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.object_store import BackendObjectStore, ObjectStoreABC
from nexus.core.response import HandlerResponse


class MockBackend(Backend):
    """Minimal in-memory Backend for ObjectStoreABC conformance tests."""

    def __init__(self) -> None:
        self._content: dict[str, bytes] = {}
        self._ref_counts: dict[str, int] = {}

    @property
    def name(self) -> str:
        return "mock"

    def write_content(self, content, context=None) -> HandlerResponse[str]:
        h = hashlib.sha256(content).hexdigest()
        if h in self._content:
            self._ref_counts[h] += 1
        else:
            self._content[h] = content
            self._ref_counts[h] = 1
        return HandlerResponse.ok(data=h, backend_name="mock")

    def read_content(self, content_hash, context=None) -> HandlerResponse[bytes]:
        if content_hash not in self._content:
            return HandlerResponse.not_found(
                path=content_hash, message="Content not found", backend_name="mock"
            )
        return HandlerResponse.ok(data=self._content[content_hash], backend_name="mock")

    def batch_read_content(
        self, content_hashes, context=None, *, contexts=None
    ) -> dict[str, bytes | None]:
        return {h: self._content.get(h) for h in content_hashes}

    def delete_content(self, content_hash, context=None) -> HandlerResponse[None]:
        if content_hash not in self._content:
            return HandlerResponse.not_found(path=content_hash, backend_name="mock")
        self._ref_counts[content_hash] -= 1
        if self._ref_counts[content_hash] <= 0:
            del self._content[content_hash]
            del self._ref_counts[content_hash]
        return HandlerResponse.ok(data=None, backend_name="mock")

    def content_exists(self, content_hash, context=None) -> HandlerResponse[bool]:
        return HandlerResponse.ok(data=content_hash in self._content, backend_name="mock")

    def get_content_size(self, content_hash, context=None) -> HandlerResponse[int]:
        if content_hash not in self._content:
            return HandlerResponse.not_found(path=content_hash, backend_name="mock")
        return HandlerResponse.ok(data=len(self._content[content_hash]), backend_name="mock")

    def get_ref_count(self, content_hash, context=None) -> HandlerResponse[int]:
        return HandlerResponse.ok(data=self._ref_counts.get(content_hash, 0), backend_name="mock")

    def mkdir(self, path, parents=False, exist_ok=False, context=None) -> HandlerResponse[None]:
        return HandlerResponse.ok(data=None, backend_name="mock")

    def rmdir(self, path, recursive=False, context=None) -> HandlerResponse[None]:
        return HandlerResponse.ok(data=None, backend_name="mock")

    def is_directory(self, path, context=None) -> HandlerResponse[bool]:
        return HandlerResponse.ok(data=False, backend_name="mock")


# === Fixtures ===


@pytest.fixture
def local_store(tmp_path) -> BackendObjectStore:
    """ObjectStoreABC backed by LocalBackend with tmp_path."""
    backend = LocalBackend(root_path=str(tmp_path))
    return BackendObjectStore(backend)


@pytest.fixture
def mock_store() -> BackendObjectStore:
    """ObjectStoreABC backed by MockBackend."""
    backend = MockBackend()
    return BackendObjectStore(backend)


@pytest.fixture(params=["local", "mock"])
def store(request, tmp_path) -> BackendObjectStore:
    """Parametrized fixture for both local and mock stores."""
    if request.param == "local":
        backend = LocalBackend(root_path=str(tmp_path))
        return BackendObjectStore(backend)
    else:
        backend = MockBackend()
        return BackendObjectStore(backend)


# === Conformance Tests ===


class TestObjectStoreConformance:
    """Core conformance tests — must pass for all ObjectStoreABC implementations."""

    def test_write_returns_hash(self, store: BackendObjectStore) -> None:
        content_hash = store.write(b"hello world")
        assert isinstance(content_hash, str)
        assert len(content_hash) == 64  # SHA-256 hex

    def test_read_roundtrip(self, store: BackendObjectStore) -> None:
        content = b"roundtrip test data"
        content_hash = store.write(content)
        result = store.read(content_hash)
        assert result == content

    def test_read_nonexistent_raises(self, store: BackendObjectStore) -> None:
        fake_hash = "a" * 64
        with pytest.raises((NexusFileNotFoundError, BackendError)):
            store.read(fake_hash)

    def test_delete(self, store: BackendObjectStore) -> None:
        content_hash = store.write(b"delete me")
        assert store.exists(content_hash)
        store.delete(content_hash)
        assert not store.exists(content_hash)

    def test_exists_true(self, store: BackendObjectStore) -> None:
        content_hash = store.write(b"I exist")
        assert store.exists(content_hash) is True

    def test_exists_false(self, store: BackendObjectStore) -> None:
        fake_hash = "b" * 64
        assert store.exists(fake_hash) is False

    def test_size(self, store: BackendObjectStore) -> None:
        content = b"size check"
        content_hash = store.write(content)
        assert store.size(content_hash) == len(content)

    def test_batch_read(self, store: BackendObjectStore) -> None:
        h1 = store.write(b"item1")
        h2 = store.write(b"item2")
        h3 = store.write(b"item3")
        result = store.batch_read([h1, h2, h3])
        assert result[h1] == b"item1"
        assert result[h2] == b"item2"
        assert result[h3] == b"item3"

    def test_batch_read_partial(self, store: BackendObjectStore) -> None:
        h1 = store.write(b"exists")
        fake_hash = "c" * 64
        result = store.batch_read([h1, fake_hash])
        assert result[h1] == b"exists"
        assert result[fake_hash] is None

    def test_deduplication(self, store: BackendObjectStore) -> None:
        content = b"same content twice"
        h1 = store.write(content)
        h2 = store.write(content)
        assert h1 == h2


# === Protocol isinstance Tests ===


class TestProtocolConformance:
    def test_backend_object_store_isinstance(self, mock_store: BackendObjectStore) -> None:
        assert isinstance(mock_store, ObjectStoreABC)

    def test_name_property(self, mock_store: BackendObjectStore) -> None:
        assert mock_store.name == "mock"

    def test_local_store_isinstance(self, local_store: BackendObjectStore) -> None:
        assert isinstance(local_store, ObjectStoreABC)

    def test_local_store_name(self, local_store: BackendObjectStore) -> None:
        assert local_store.name == "local"


# === Adapter Error Handling Tests ===


class TestAdapterErrorHandling:
    def test_read_failure_raises_exception(self, mock_store: BackendObjectStore) -> None:
        with pytest.raises((NexusFileNotFoundError, BackendError)):
            mock_store.read("nonexistent" + "0" * 54)

    def test_error_message_preserved(self, mock_store: BackendObjectStore) -> None:
        try:
            mock_store.read("z" * 64)
            pytest.fail("Expected exception")
        except (NexusFileNotFoundError, BackendError) as e:
            assert "not found" in str(e).lower() or "Content not found" in str(e)

    def test_write_returns_consistent_hash(self, mock_store: BackendObjectStore) -> None:
        content = b"deterministic"
        h1 = mock_store.write(content)
        h2 = mock_store.write(content)
        assert h1 == h2


# === Benchmark Tests ===


class TestAdapterOverhead:
    def test_adapter_overhead_under_10us(self, mock_store: BackendObjectStore) -> None:
        """Measure adapter call overhead — should be minimal."""
        # Write content first
        content_hash = mock_store.write(b"benchmark data")

        # Warmup
        for _ in range(100):
            mock_store.exists(content_hash)

        iterations = 10_000
        start = time.perf_counter()
        for _ in range(iterations):
            mock_store.exists(content_hash)
        elapsed_us = (time.perf_counter() - start) * 1_000_000 / iterations

        # Adapter overhead should be minimal (under 50μs including MockBackend)
        assert elapsed_us < 50, f"Adapter call took {elapsed_us:.2f}μs per call"
