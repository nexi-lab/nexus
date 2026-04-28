"""Benchmark tests for ObjectStoreABC backend overhead.

Ensures direct Backend method calls (write, read, exists) stay fast.
Covers: WriteResult creation, direct exception-based returns,
and Backend methods (read_content, write_content, delete_content).
"""

import hashlib
import time

import pytest

from nexus.backends.base.backend import Backend
from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.core.object_store import ObjectStoreABC, WriteResult


class _BenchBackend(Backend):
    """Minimal zero-overhead backend for isolating call cost."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    @property
    def name(self) -> str:
        return "bench"

    def write_content(
        self, content, content_id: str = "", *, offset: int = 0, context=None
    ) -> WriteResult:
        h = hashlib.sha256(content).hexdigest()
        self._store[h] = content
        return WriteResult(content_id=h, size=len(content))

    def read_content(self, content_id, context=None) -> bytes:
        data = self._store.get(content_id)
        if data is None:
            raise NexusFileNotFoundError(path=content_id, message="Not found")
        return data

    def batch_read_content(
        self, content_ids, context=None, *, contexts=None
    ) -> dict[str, bytes | None]:
        return {h: self._store.get(h) for h in content_ids}

    def delete_content(self, content_id, context=None) -> None:
        self._store.pop(content_id, None)

    def get_content_size(self, content_id, context=None) -> int:
        data = self._store.get(content_id)
        if data is None:
            raise NexusFileNotFoundError(path=content_id, message="Not found")
        return len(data)

    def mkdir(self, path, parents=False, exist_ok=False, context=None) -> None:
        pass

    def rmdir(self, path, recursive=False, context=None) -> None:
        pass


@pytest.fixture
def bench_backend() -> _BenchBackend:
    return _BenchBackend()


class TestWriteResultOverhead:
    """Benchmark WriteResult creation overhead."""

    def test_write_result_creation_speed(self) -> None:
        """Raw WriteResult creation should be very fast."""
        iterations = 10_000
        start = time.perf_counter()
        for _ in range(iterations):
            WriteResult(content_id="a" * 64, size=100)
        elapsed_us = (time.perf_counter() - start) * 1_000_000 / iterations
        assert elapsed_us < 10, f"WriteResult() took {elapsed_us:.2f}us per call"


class TestBackendOverhead:
    """Benchmark Backend direct method overhead for hot-path operations."""

    def test_backend_import_succeeds(self) -> None:
        """Verify ObjectStoreABC can be imported."""
        assert ObjectStoreABC is not None
        assert Backend is not None

    def test_write_overhead(self, bench_backend: _BenchBackend) -> None:
        """write_content() overhead: hash + WriteResult on return."""
        # Warmup
        for i in range(100):
            bench_backend.write_content(f"warmup-{i}".encode())

        iterations = 5_000
        content = b"benchmark write payload"
        start = time.perf_counter()
        for _ in range(iterations):
            bench_backend.write_content(content)
        elapsed_us = (time.perf_counter() - start) * 1_000_000 / iterations

        # write_content() includes SHA-256 hashing + WriteResult creation
        assert elapsed_us < 100, f"write_content() took {elapsed_us:.2f}us per call"

    def test_read_overhead(self, bench_backend: _BenchBackend) -> None:
        """read_content() overhead: direct bytes return."""
        result = bench_backend.write_content(b"benchmark read payload")
        content_id = result.content_id

        # Warmup
        for _ in range(100):
            bench_backend.read_content(content_id)

        iterations = 10_000
        start = time.perf_counter()
        for _ in range(iterations):
            bench_backend.read_content(content_id)
        elapsed_us = (time.perf_counter() - start) * 1_000_000 / iterations

        # read_content() = dict lookup + direct bytes return
        assert elapsed_us < 50, f"read_content() took {elapsed_us:.2f}us per call"

    def test_get_content_size_overhead(self, bench_backend: _BenchBackend) -> None:
        """get_content_size() overhead: direct int return (lightest op)."""
        result = bench_backend.write_content(b"benchmark size payload")
        content_id = result.content_id

        for _ in range(100):
            bench_backend.get_content_size(content_id)

        iterations = 10_000
        start = time.perf_counter()
        for _ in range(iterations):
            bench_backend.get_content_size(content_id)
        elapsed_us = (time.perf_counter() - start) * 1_000_000 / iterations

        assert elapsed_us < 50, f"get_content_size() took {elapsed_us:.2f}us per call"
