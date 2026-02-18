"""Conformance test suite for ObjectStoreABC Protocol.

Tests:
- ObjectStoreABC conformance (write/read/delete/exists/size/batch_read)
- BackendObjectStore adapter error handling
- Protocol isinstance checks
- Deduplication behavior
- Edge cases (empty, binary, large content)
- Hash validation
- Context propagation
- Repr and debuggability

Parametrized across LocalBackend and MockBackend.
"""


import hashlib
import time
from unittest.mock import MagicMock

import pytest

from nexus.backends.backend import Backend
from nexus.backends.local import LocalBackend
from nexus.core.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.object_store import BackendObjectStore, ObjectStoreABC, _validate_hash
from nexus.core.response import HandlerResponse


class MockBackend(Backend):
    """Minimal in-memory Backend for ObjectStoreABC conformance tests."""

    def __init__(self) -> None:
        self._content: dict[str, bytes] = {}
        self._ref_counts: dict[str, int] = {}
        self._last_context: object = None  # Spy: captures context for assertions

    @property
    def name(self) -> str:
        return "mock"

    def write_content(self, content, context=None) -> HandlerResponse[str]:
        self._last_context = context
        h = hashlib.sha256(content).hexdigest()
        if h in self._content:
            self._ref_counts[h] += 1
        else:
            self._content[h] = content
            self._ref_counts[h] = 1
        return HandlerResponse.ok(data=h, backend_name="mock")

    def read_content(self, content_hash, context=None) -> HandlerResponse[bytes]:
        self._last_context = context
        if content_hash not in self._content:
            return HandlerResponse.not_found(
                path=content_hash, message="Content not found", backend_name="mock"
            )
        return HandlerResponse.ok(data=self._content[content_hash], backend_name="mock")

    def batch_read_content(
        self, content_hashes, context=None, *, contexts=None
    ) -> dict[str, bytes | None]:
        self._last_context = context
        return {h: self._content.get(h) for h in content_hashes}

    def delete_content(self, content_hash, context=None) -> HandlerResponse[None]:
        self._last_context = context
        if content_hash not in self._content:
            return HandlerResponse.not_found(path=content_hash, backend_name="mock")
        self._ref_counts[content_hash] -= 1
        if self._ref_counts[content_hash] <= 0:
            del self._content[content_hash]
            del self._ref_counts[content_hash]
        return HandlerResponse.ok(data=None, backend_name="mock")

    def content_exists(self, content_hash, context=None) -> HandlerResponse[bool]:
        self._last_context = context
        return HandlerResponse.ok(data=content_hash in self._content, backend_name="mock")

    def get_content_size(self, content_hash, context=None) -> HandlerResponse[int]:
        self._last_context = context
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
def mock_backend() -> MockBackend:
    """Raw MockBackend for spy-based assertions."""
    return MockBackend()


@pytest.fixture
def mock_store(mock_backend) -> BackendObjectStore:
    """ObjectStoreABC backed by MockBackend."""
    return BackendObjectStore(mock_backend)


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


# === Edge Case Tests (Issue 9A) ===


class TestEdgeCases:
    """Edge cases that validate adapter boundary behavior."""

    def test_empty_content(self, store: BackendObjectStore) -> None:
        """Empty content (0 bytes) should hash consistently and roundtrip."""
        content = b""
        content_hash = store.write(content)
        assert isinstance(content_hash, str)
        assert len(content_hash) == 64
        retrieved = store.read(content_hash)
        assert retrieved == b""
        assert store.size(content_hash) == 0

    def test_binary_content_all_bytes(self, store: BackendObjectStore) -> None:
        """Binary content with all 256 byte values should preserve exactly."""
        content = bytes(range(256))
        content_hash = store.write(content)
        retrieved = store.read(content_hash)
        assert retrieved == content
        assert store.size(content_hash) == 256

    def test_large_content(self, store: BackendObjectStore) -> None:
        """Large content (1MB) should handle without memory issues."""
        content = b"X" * (1024 * 1024)
        content_hash = store.write(content)
        retrieved = store.read(content_hash)
        assert len(retrieved) == len(content)
        assert store.size(content_hash) == 1024 * 1024

    def test_batch_read_empty_list(self, store: BackendObjectStore) -> None:
        """batch_read([]) returns empty dict."""
        result = store.batch_read([])
        assert result == {}

    def test_batch_read_single_item(self, store: BackendObjectStore) -> None:
        """batch_read([hash]) works like read()."""
        content = b"single"
        content_hash = store.write(content)
        result = store.batch_read([content_hash])
        assert len(result) == 1
        assert result[content_hash] == content

    def test_batch_read_all_missing(self, store: BackendObjectStore) -> None:
        """batch_read with all missing hashes returns all None."""
        result = store.batch_read(["a" * 64, "b" * 64, "c" * 64])
        assert len(result) == 3
        assert all(v is None for v in result.values())

    def test_size_consistency_roundtrip(self, store: BackendObjectStore) -> None:
        """size() matches len(read()) immediately after write()."""
        content = b"consistency check content"
        content_hash = store.write(content)
        assert store.size(content_hash) == len(content)
        retrieved = store.read(content_hash)
        assert len(retrieved) == store.size(content_hash)


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


# === Adapter Error Handling Tests (Issue 10A) ===


class TestAdapterErrorHandling:
    def test_read_nonexistent_raises(self, mock_store: BackendObjectStore) -> None:
        with pytest.raises((NexusFileNotFoundError, BackendError)):
            mock_store.read("0" * 64)

    def test_delete_nonexistent_raises(self, mock_store: BackendObjectStore) -> None:
        with pytest.raises((NexusFileNotFoundError, BackendError)):
            mock_store.delete("0" * 64)

    def test_size_nonexistent_raises(self, mock_store: BackendObjectStore) -> None:
        with pytest.raises((NexusFileNotFoundError, BackendError)):
            mock_store.size("0" * 64)

    def test_error_message_preserved(self, mock_store: BackendObjectStore) -> None:
        try:
            mock_store.read("0" * 64)
            pytest.fail("Expected exception")
        except (NexusFileNotFoundError, BackendError) as e:
            assert "not found" in str(e).lower() or "Content not found" in str(e)

    def test_write_returns_consistent_hash(self, mock_store: BackendObjectStore) -> None:
        content = b"deterministic"
        h1 = mock_store.write(content)
        h2 = mock_store.write(content)
        assert h1 == h2


# === Hash Validation Tests (Issue 5A) ===


class TestHashValidation:
    """Validates _validate_hash rejects malformed hashes at adapter boundary."""

    def test_validate_hash_accepts_valid(self) -> None:
        _validate_hash("a" * 64)
        _validate_hash("0123456789abcdef" * 4)

    def test_validate_hash_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="Invalid SHA-256"):
            _validate_hash("")

    def test_validate_hash_rejects_short(self) -> None:
        with pytest.raises(ValueError, match="Invalid SHA-256"):
            _validate_hash("abc")

    def test_validate_hash_rejects_uppercase(self) -> None:
        with pytest.raises(ValueError, match="Invalid SHA-256"):
            _validate_hash("A" * 64)

    def test_validate_hash_rejects_non_hex(self) -> None:
        with pytest.raises(ValueError, match="Invalid SHA-256"):
            _validate_hash("g" * 64)

    def test_validate_hash_rejects_too_long(self) -> None:
        with pytest.raises(ValueError, match="Invalid SHA-256"):
            _validate_hash("a" * 65)

    def test_read_rejects_invalid_hash(self, mock_store: BackendObjectStore) -> None:
        with pytest.raises(ValueError, match="Invalid SHA-256"):
            mock_store.read("not-a-hash")

    def test_delete_rejects_invalid_hash(self, mock_store: BackendObjectStore) -> None:
        with pytest.raises(ValueError, match="Invalid SHA-256"):
            mock_store.delete("xyz")

    def test_exists_rejects_invalid_hash(self, mock_store: BackendObjectStore) -> None:
        with pytest.raises(ValueError, match="Invalid SHA-256"):
            mock_store.exists("")

    def test_size_rejects_invalid_hash(self, mock_store: BackendObjectStore) -> None:
        with pytest.raises(ValueError, match="Invalid SHA-256"):
            mock_store.size("A" * 64)

    def test_batch_read_rejects_invalid_hash(self, mock_store: BackendObjectStore) -> None:
        valid = "a" * 64
        invalid = "ZZZZ"
        with pytest.raises(ValueError, match="Invalid SHA-256"):
            mock_store.batch_read([valid, invalid])


# === Context Propagation Tests (Issue 11A) ===


class TestContextPropagation:
    """Verify OperationContext flows from adapter to backend."""

    def test_context_none_by_default(self, mock_backend: MockBackend) -> None:
        store = BackendObjectStore(mock_backend)
        store.write(b"test")
        assert mock_backend._last_context is None

    def test_context_propagated_to_write(self, mock_backend: MockBackend) -> None:
        ctx = MagicMock()
        store = BackendObjectStore(mock_backend, context=ctx)
        store.write(b"test")
        assert mock_backend._last_context is ctx

    def test_context_propagated_to_read(self, mock_backend: MockBackend) -> None:
        ctx = MagicMock()
        store_no_ctx = BackendObjectStore(mock_backend)
        content_hash = store_no_ctx.write(b"ctx read test")

        store = BackendObjectStore(mock_backend, context=ctx)
        store.read(content_hash)
        assert mock_backend._last_context is ctx

    def test_context_propagated_to_exists(self, mock_backend: MockBackend) -> None:
        ctx = MagicMock()
        store = BackendObjectStore(mock_backend, context=ctx)
        store.exists("a" * 64)
        assert mock_backend._last_context is ctx

    def test_context_propagated_to_delete(self, mock_backend: MockBackend) -> None:
        ctx = MagicMock()
        store_no_ctx = BackendObjectStore(mock_backend)
        content_hash = store_no_ctx.write(b"ctx delete test")

        store = BackendObjectStore(mock_backend, context=ctx)
        store.delete(content_hash)
        assert mock_backend._last_context is ctx

    def test_context_propagated_to_batch_read(self, mock_backend: MockBackend) -> None:
        ctx = MagicMock()
        store = BackendObjectStore(mock_backend, context=ctx)
        store.batch_read(["a" * 64])
        assert mock_backend._last_context is ctx


# === Repr and Debuggability Tests (Issue 6A) ===


class TestReprAndDebug:
    """Verify __repr__ and read-only properties work correctly."""

    def test_repr_without_context(self, mock_store: BackendObjectStore) -> None:
        r = repr(mock_store)
        assert "BackendObjectStore" in r
        assert "mock" in r
        assert "context" not in r

    def test_repr_with_context(self, mock_backend: MockBackend) -> None:
        ctx = MagicMock()
        store = BackendObjectStore(mock_backend, context=ctx)
        r = repr(store)
        assert "BackendObjectStore" in r
        assert "mock" in r
        assert "context=" in r

    def test_backend_property_readonly(self, mock_backend: MockBackend) -> None:
        store = BackendObjectStore(mock_backend)
        assert store.backend is mock_backend

    def test_backend_property_returns_backend_type(self, local_store: BackendObjectStore) -> None:
        assert isinstance(local_store.backend, LocalBackend)


# === Benchmark Tests ===


class TestAdapterOverhead:
    def test_adapter_overhead_under_50us(self, mock_store: BackendObjectStore) -> None:
        """Measure adapter call overhead — should be minimal."""
        content_hash = mock_store.write(b"benchmark data")

        # Warmup
        for _ in range(100):
            mock_store.exists(content_hash)

        iterations = 10_000
        start = time.perf_counter()
        for _ in range(iterations):
            mock_store.exists(content_hash)
        elapsed_us = (time.perf_counter() - start) * 1_000_000 / iterations

        # Adapter overhead should be minimal (under 50μs including MockBackend + validation)
        assert elapsed_us < 50, f"Adapter call took {elapsed_us:.2f}μs per call"
