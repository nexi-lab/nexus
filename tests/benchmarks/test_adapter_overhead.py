"""Benchmark tests for ObjectStoreABC adapter overhead.

Ensures the adapter + @timed_response add < 50μs per call.
Covers: HandlerResponse creation, @timed_response decorator, and
BackendObjectStore adapter methods (read, write, exists).
"""

import hashlib
import time

import pytest

from nexus.backends.backend import Backend
from nexus.core.object_store import BackendObjectStore, ObjectStoreABC
from nexus.core.response import HandlerResponse, timed_response

class _BenchBackend(Backend):
    """Minimal zero-overhead backend for isolating adapter cost."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    @property
    def name(self) -> str:
        return "bench"

    def write_content(self, content, context=None) -> HandlerResponse[str]:
        h = hashlib.sha256(content).hexdigest()
        self._store[h] = content
        return HandlerResponse.ok(data=h, backend_name="bench")

    def read_content(self, content_hash, context=None) -> HandlerResponse[bytes]:
        data = self._store.get(content_hash)
        if data is None:
            return HandlerResponse.not_found(path=content_hash, backend_name="bench")
        return HandlerResponse.ok(data=data, backend_name="bench")

    def batch_read_content(
        self, content_hashes, context=None, *, contexts=None
    ) -> dict[str, bytes | None]:
        return {h: self._store.get(h) for h in content_hashes}

    def delete_content(self, content_hash, context=None) -> HandlerResponse[None]:
        self._store.pop(content_hash, None)
        return HandlerResponse.ok(data=None, backend_name="bench")

    def content_exists(self, content_hash, context=None) -> HandlerResponse[bool]:
        return HandlerResponse.ok(data=content_hash in self._store, backend_name="bench")

    def get_content_size(self, content_hash, context=None) -> HandlerResponse[int]:
        data = self._store.get(content_hash)
        if data is None:
            return HandlerResponse.not_found(path=content_hash, backend_name="bench")
        return HandlerResponse.ok(data=len(data), backend_name="bench")

    def get_ref_count(self, content_hash, context=None) -> HandlerResponse[int]:
        return HandlerResponse.ok(
            data=1 if content_hash in self._store else 0, backend_name="bench"
        )

    def mkdir(self, path, parents=False, exist_ok=False, context=None) -> HandlerResponse[None]:
        return HandlerResponse.ok(data=None, backend_name="bench")

    def rmdir(self, path, recursive=False, context=None) -> HandlerResponse[None]:
        return HandlerResponse.ok(data=None, backend_name="bench")

    def is_directory(self, path, context=None) -> HandlerResponse[bool]:
        return HandlerResponse.ok(data=False, backend_name="bench")

@pytest.fixture
def bench_store() -> BackendObjectStore:
    backend = _BenchBackend()
    return BackendObjectStore(backend)

class TestTimedResponseOverhead:
    """Benchmark @timed_response decorator overhead."""

    def test_handler_response_creation_speed(self) -> None:
        """Raw HandlerResponse.ok() creation should be very fast."""
        iterations = 10_000
        start = time.perf_counter()
        for _ in range(iterations):
            HandlerResponse.ok(data="hash123", backend_name="bench")
        elapsed_us = (time.perf_counter() - start) * 1_000_000 / iterations
        assert elapsed_us < 10, f"HandlerResponse.ok() took {elapsed_us:.2f}μs per call"

    def test_timed_response_decorator_overhead(self) -> None:
        """@timed_response overhead should be < 50μs per call."""

        class Bench:
            name = "bench"

            @timed_response
            def op(self) -> HandlerResponse[str]:
                return HandlerResponse.ok(data="ok", backend_name=self.name)

        obj = Bench()
        for _ in range(100):
            obj.op()

        iterations = 10_000
        start = time.perf_counter()
        for _ in range(iterations):
            obj.op()
        elapsed_us = (time.perf_counter() - start) * 1_000_000 / iterations
        assert elapsed_us < 50, f"@timed_response took {elapsed_us:.2f}μs per call"

class TestAdapterOverhead:
    """Benchmark BackendObjectStore adapter overhead for hot-path operations."""

    def test_adapter_import_succeeds(self) -> None:
        """Verify ObjectStoreABC can be imported."""
        assert ObjectStoreABC is not None
        assert BackendObjectStore is not None

    def test_write_overhead(self, bench_store: BackendObjectStore) -> None:
        """write() adapter overhead: adapter + hash validation on return."""
        # Warmup
        for i in range(100):
            bench_store.write(f"warmup-{i}".encode())

        iterations = 5_000
        content = b"benchmark write payload"
        start = time.perf_counter()
        for _ in range(iterations):
            bench_store.write(content)
        elapsed_us = (time.perf_counter() - start) * 1_000_000 / iterations

        # write() includes SHA-256 hashing in backend + adapter .unwrap()
        assert elapsed_us < 100, f"write() took {elapsed_us:.2f}μs per call"

    def test_read_overhead(self, bench_store: BackendObjectStore) -> None:
        """read() adapter overhead: validation + unwrap."""
        content_hash = bench_store.write(b"benchmark read payload")

        # Warmup
        for _ in range(100):
            bench_store.read(content_hash)

        iterations = 10_000
        start = time.perf_counter()
        for _ in range(iterations):
            bench_store.read(content_hash)
        elapsed_us = (time.perf_counter() - start) * 1_000_000 / iterations

        # read() = hash validation + backend.read_content + .unwrap()
        assert elapsed_us < 50, f"read() took {elapsed_us:.2f}μs per call"

    def test_exists_overhead(self, bench_store: BackendObjectStore) -> None:
        """exists() adapter overhead: validation + unwrap (lightest op)."""
        content_hash = bench_store.write(b"benchmark exists payload")

        for _ in range(100):
            bench_store.exists(content_hash)

        iterations = 10_000
        start = time.perf_counter()
        for _ in range(iterations):
            bench_store.exists(content_hash)
        elapsed_us = (time.perf_counter() - start) * 1_000_000 / iterations

        assert elapsed_us < 50, f"exists() took {elapsed_us:.2f}μs per call"
