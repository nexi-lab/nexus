"""Conformance test suite for ObjectStoreABC Protocol.

Tests:
- ObjectStoreABC conformance (write_content/read_content/delete_content/get_content_size/batch_read_content)
- Error handling on direct backend calls
- Protocol isinstance checks
- Deduplication behavior
- Edge cases (empty, binary, large content)
- Hash validation
- Context propagation (direct method parameter)
- Backend call overhead

Parametrized across CASLocalBackend and MockBackend.
"""

import hashlib
import time
from unittest.mock import MagicMock

import pytest

from nexus.backends.base.backend import Backend
from nexus.backends.base.cas_addressing_engine import _validate_hash
from nexus.backends.storage.cas_local import CASLocalBackend
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.object_store import ObjectStoreABC, WriteResult


class MockBackend(Backend):
    """Minimal in-memory Backend for ObjectStoreABC conformance tests."""

    def __init__(self) -> None:
        self._content: dict[str, bytes] = {}
        self._ref_counts: dict[str, int] = {}
        self._last_context: object = None  # Spy: captures context for assertions

    @property
    def name(self) -> str:
        return "mock"

    def write_content(
        self, content, content_id: str = "", *, offset: int = 0, context=None
    ) -> WriteResult:
        self._last_context = context
        h = hashlib.sha256(content).hexdigest()
        if h in self._content:
            self._ref_counts[h] += 1
        else:
            self._content[h] = content
            self._ref_counts[h] = 1
        return WriteResult(content_id=h, size=len(content))

    def read_content(self, content_id, context=None) -> bytes:
        self._last_context = context
        if content_id not in self._content:
            raise NexusFileNotFoundError(path=content_id, message="Content not found")
        return self._content[content_id]

    def batch_read_content(
        self, content_ids, context=None, *, contexts=None
    ) -> dict[str, bytes | None]:
        self._last_context = context
        return {h: self._content.get(h) for h in content_ids}

    def delete_content(self, content_id, context=None) -> None:
        self._last_context = context
        if content_id not in self._content:
            raise NexusFileNotFoundError(path=content_id, message="Content not found")
        self._ref_counts[content_id] -= 1
        if self._ref_counts[content_id] <= 0:
            del self._content[content_id]
            del self._ref_counts[content_id]

    def content_exists(self, content_id, context=None) -> bool:
        self._last_context = context
        return content_id in self._content

    def get_content_size(self, content_id, context=None) -> int:
        self._last_context = context
        if content_id not in self._content:
            raise NexusFileNotFoundError(path=content_id, message="Content not found")
        return len(self._content[content_id])

    def mkdir(self, path, parents=False, exist_ok=False, context=None) -> None:
        return None

    def rmdir(self, path, recursive=False, context=None) -> None:
        return None

    def is_directory(self, path, context=None) -> bool:
        return False


class DirectObjectStore(ObjectStoreABC):
    """Minimal direct ObjectStoreABC implementation for default method tests."""

    def __init__(self) -> None:
        self._content: dict[str, bytes] = {}

    @property
    def name(self) -> str:
        return "direct"

    def write_content(
        self, content, content_id: str = "", *, offset: int = 0, context=None
    ) -> WriteResult:
        key = content_id or hashlib.sha256(content).hexdigest()
        self._content[key] = content
        return WriteResult(content_id=key, size=len(content))

    def read_content(self, content_id, context=None) -> bytes:
        if content_id not in self._content:
            raise NexusFileNotFoundError(path=content_id, message="Content not found")
        return self._content[content_id]

    def delete_content(self, content_id, context=None) -> None:
        if content_id not in self._content:
            raise NexusFileNotFoundError(path=content_id, message="Content not found")
        del self._content[content_id]

    def get_content_size(self, content_id, context=None) -> int:
        return len(self.read_content(content_id, context=context))

    def mkdir(self, path, parents=False, exist_ok=False, context=None) -> None:
        return None

    def rmdir(self, path, recursive=False, context=None) -> None:
        return None


# === Fixtures ===


@pytest.fixture
def local_store(tmp_path) -> ObjectStoreABC:
    """ObjectStoreABC backed by CASLocalBackend with tmp_path."""
    return CASLocalBackend(root_path=str(tmp_path))


@pytest.fixture
def mock_backend() -> MockBackend:
    """Raw MockBackend for spy-based assertions."""
    return MockBackend()


@pytest.fixture
def mock_store() -> MockBackend:
    """MockBackend as ObjectStoreABC (Backend IS ObjectStoreABC now)."""
    return MockBackend()


@pytest.fixture(params=["backend", "object_store"])
def default_stream_store(request) -> ObjectStoreABC:
    """Stores that exercise Backend and direct ObjectStoreABC stream defaults."""
    if request.param == "backend":
        return MockBackend()
    return DirectObjectStore()


@pytest.fixture(params=["local", "mock"])
def store(request, tmp_path) -> ObjectStoreABC:
    """Parametrized fixture for both local and mock stores."""
    if request.param == "local":
        return CASLocalBackend(root_path=str(tmp_path))
    else:
        return MockBackend()


# === Conformance Tests ===


class TestObjectStoreConformance:
    """Core conformance tests -- must pass for all ObjectStoreABC implementations."""

    def test_write_returns_write_result(self, store: ObjectStoreABC) -> None:
        result = store.write_content(b"hello world")
        assert isinstance(result, WriteResult)
        assert isinstance(result.content_id, str)
        assert result.size == len(b"hello world")

    def test_read_roundtrip(self, store: ObjectStoreABC) -> None:
        content = b"roundtrip test data"
        result = store.write_content(content)
        retrieved = store.read_content(result.content_id)
        assert retrieved == content

    def test_read_nonexistent_raises(self, store: ObjectStoreABC) -> None:
        fake_hash = "a" * 64
        with pytest.raises((NexusFileNotFoundError, BackendError)):
            store.read_content(fake_hash)

    def test_delete(self, store: ObjectStoreABC) -> None:
        result = store.write_content(b"delete me")
        content_id = result.content_id
        # Verify content exists via read
        store.read_content(content_id)
        # Delete it
        store.delete_content(content_id)
        # Verify it's gone
        with pytest.raises((NexusFileNotFoundError, BackendError)):
            store.read_content(content_id)

    def test_get_content_size(self, store: ObjectStoreABC) -> None:
        content = b"size check"
        result = store.write_content(content)
        assert store.get_content_size(result.content_id) == len(content)

    def test_batch_read_content(self, store: ObjectStoreABC) -> None:
        h1 = store.write_content(b"item1").content_id
        h2 = store.write_content(b"item2").content_id
        h3 = store.write_content(b"item3").content_id
        result = store.batch_read_content([h1, h2, h3])
        assert result[h1] == b"item1"
        assert result[h2] == b"item2"
        assert result[h3] == b"item3"

    def test_batch_read_content_partial(self, store: ObjectStoreABC) -> None:
        h1 = store.write_content(b"exists").content_id
        fake_hash = "c" * 64
        result = store.batch_read_content([h1, fake_hash])
        assert result[h1] == b"exists"
        assert result[fake_hash] is None

    def test_deduplication(self, store: ObjectStoreABC) -> None:
        content = b"same content twice"
        h1 = store.write_content(content).content_id
        h2 = store.write_content(content).content_id
        assert h1 == h2


# === Edge Case Tests (Issue 9A) ===


class TestEdgeCases:
    """Edge cases that validate boundary behavior."""

    def test_empty_content(self, store: ObjectStoreABC) -> None:
        """Empty content (0 bytes) should hash consistently and roundtrip."""
        content = b""
        result = store.write_content(content)
        assert isinstance(result.content_id, str)
        retrieved = store.read_content(result.content_id)
        assert retrieved == b""
        assert store.get_content_size(result.content_id) == 0

    def test_binary_content_all_bytes(self, store: ObjectStoreABC) -> None:
        """Binary content with all 256 byte values should preserve exactly."""
        content = bytes(range(256))
        result = store.write_content(content)
        retrieved = store.read_content(result.content_id)
        assert retrieved == content
        assert store.get_content_size(result.content_id) == 256

    def test_large_content(self, store: ObjectStoreABC) -> None:
        """Large content (1MB) should handle without memory issues."""
        content = b"X" * (1024 * 1024)
        result = store.write_content(content)
        retrieved = store.read_content(result.content_id)
        assert len(retrieved) == len(content)
        assert store.get_content_size(result.content_id) == 1024 * 1024

    def test_batch_read_content_empty_list(self, store: ObjectStoreABC) -> None:
        """batch_read_content([]) returns empty dict."""
        result = store.batch_read_content([])
        assert result == {}

    def test_batch_read_content_single_item(self, store: ObjectStoreABC) -> None:
        """batch_read_content([hash]) works like read_content()."""
        content = b"single"
        content_id = store.write_content(content).content_id
        result = store.batch_read_content([content_id])
        assert len(result) == 1
        assert result[content_id] == content

    def test_batch_read_content_all_missing(self, store: ObjectStoreABC) -> None:
        """batch_read_content with all missing hashes returns all None."""
        result = store.batch_read_content(["a" * 64, "b" * 64, "c" * 64])
        assert len(result) == 3
        assert all(v is None for v in result.values())

    def test_size_consistency_roundtrip(self, store: ObjectStoreABC) -> None:
        """get_content_size() matches len(read_content()) immediately after write_content()."""
        content = b"consistency check content"
        content_id = store.write_content(content).content_id
        assert store.get_content_size(content_id) == len(content)
        retrieved = store.read_content(content_id)
        assert len(retrieved) == store.get_content_size(content_id)


# === Protocol isinstance Tests ===


class TestProtocolConformance:
    def test_mock_backend_isinstance(self, mock_store: MockBackend) -> None:
        assert isinstance(mock_store, ObjectStoreABC)

    def test_mock_backend_name(self, mock_store: MockBackend) -> None:
        assert mock_store.name == "mock"

    def test_local_store_isinstance(self, local_store: ObjectStoreABC) -> None:
        assert isinstance(local_store, ObjectStoreABC)

    def test_local_store_name(self, local_store: ObjectStoreABC) -> None:
        assert local_store.name == "local"


# === Error Handling Tests (Issue 10A) ===


class TestErrorHandling:
    def test_read_nonexistent_raises(self, mock_store: MockBackend) -> None:
        with pytest.raises((NexusFileNotFoundError, BackendError)):
            mock_store.read_content("0" * 64)

    def test_delete_nonexistent_raises(self, mock_store: MockBackend) -> None:
        with pytest.raises((NexusFileNotFoundError, BackendError)):
            mock_store.delete_content("0" * 64)

    def test_size_nonexistent_raises(self, mock_store: MockBackend) -> None:
        with pytest.raises((NexusFileNotFoundError, BackendError)):
            mock_store.get_content_size("0" * 64)

    def test_error_message_preserved(self, mock_store: MockBackend) -> None:
        try:
            mock_store.read_content("0" * 64)
            pytest.fail("Expected exception")
        except (NexusFileNotFoundError, BackendError) as e:
            assert "not found" in str(e).lower() or "Content not found" in str(e)

    def test_write_returns_consistent_hash(self, mock_store: MockBackend) -> None:
        content = b"deterministic"
        h1 = mock_store.write_content(content).content_id
        h2 = mock_store.write_content(content).content_id
        assert h1 == h2


class TestDefaultStreamingValidation:
    def test_stream_content_rejects_non_positive_chunk_size(
        self, default_stream_store: ObjectStoreABC
    ) -> None:
        content_id = default_stream_store.write_content(b"abcdef").content_id
        with pytest.raises(ValueError, match="chunk_size must be positive"):
            list(default_stream_store.stream_content(content_id, chunk_size=0))

    def test_stream_range_rejects_negative_start(
        self, default_stream_store: ObjectStoreABC
    ) -> None:
        content_id = default_stream_store.write_content(b"abcdef").content_id
        with pytest.raises(ValueError, match="start must be non-negative"):
            list(default_stream_store.stream_range(content_id, -1, 3))

    def test_stream_range_rejects_end_before_start(
        self, default_stream_store: ObjectStoreABC
    ) -> None:
        content_id = default_stream_store.write_content(b"abcdef").content_id
        with pytest.raises(ValueError, match="end.*must be >= start"):
            list(default_stream_store.stream_range(content_id, 4, 3))

    def test_stream_range_rejects_non_positive_chunk_size(
        self, default_stream_store: ObjectStoreABC
    ) -> None:
        content_id = default_stream_store.write_content(b"abcdef").content_id
        with pytest.raises(ValueError, match="chunk_size must be positive"):
            list(default_stream_store.stream_range(content_id, 0, 3, chunk_size=0))


# === Hash Validation Tests (Issue 5A) ===


class TestHashValidation:
    """Validates _validate_hash rejects malformed hashes."""

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


# === Context Propagation Tests (Issue 11A) ===


class TestContextPropagation:
    """Verify OperationContext flows through direct backend method calls."""

    def test_context_none_by_default(self, mock_backend: MockBackend) -> None:
        mock_backend.write_content(b"test")
        assert mock_backend._last_context is None

    def test_context_propagated_to_write(self, mock_backend: MockBackend) -> None:
        ctx = MagicMock()
        mock_backend.write_content(b"test", context=ctx)
        assert mock_backend._last_context is ctx

    def test_context_propagated_to_read(self, mock_backend: MockBackend) -> None:
        ctx = MagicMock()
        result = mock_backend.write_content(b"ctx read test")
        mock_backend.read_content(result.content_id, context=ctx)
        assert mock_backend._last_context is ctx

    def test_context_propagated_to_content_exists(self, mock_backend: MockBackend) -> None:
        ctx = MagicMock()
        mock_backend.content_exists("a" * 64, context=ctx)
        assert mock_backend._last_context is ctx

    def test_context_propagated_to_delete(self, mock_backend: MockBackend) -> None:
        ctx = MagicMock()
        result = mock_backend.write_content(b"ctx delete test")
        mock_backend.delete_content(result.content_id, context=ctx)
        assert mock_backend._last_context is ctx

    def test_context_propagated_to_batch_read(self, mock_backend: MockBackend) -> None:
        ctx = MagicMock()
        mock_backend.batch_read_content(["a" * 64], context=ctx)
        assert mock_backend._last_context is ctx


# === Benchmark Tests ===


class TestBackendOverhead:
    def test_direct_call_overhead_under_50us(self, mock_store: MockBackend) -> None:
        """Measure direct backend call overhead -- should be minimal."""
        result = mock_store.write_content(b"benchmark data")
        content_id = result.content_id

        # Warmup
        for _ in range(100):
            mock_store.read_content(content_id)

        iterations = 10_000
        start = time.perf_counter()
        for _ in range(iterations):
            mock_store.read_content(content_id)
        elapsed_us = (time.perf_counter() - start) * 1_000_000 / iterations

        # Direct backend call should be minimal (under 50us including MockBackend)
        assert elapsed_us < 50, f"Backend call took {elapsed_us:.2f}us per call"
