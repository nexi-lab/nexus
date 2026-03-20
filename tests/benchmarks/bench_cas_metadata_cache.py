"""Benchmarks for CAS metadata LRU cache (Issue #2940).

Measures the performance improvement of meta_cache on repeated
_read_meta calls, which is the hot path for is_chunked(), get_size(),
and ref_count checks.

Usage:
    uv run pytest tests/benchmarks/bench_cas_metadata_cache.py -v -o "addopts="
"""

from pathlib import Path

import pytest

from nexus.backends.storage.cas_local import CASLocalBackend


@pytest.fixture
def backend(tmp_path: Path) -> CASLocalBackend:
    return CASLocalBackend(root_path=tmp_path / "cas_bench")


class TestMetaCacheBenchmark:
    """Benchmark metadata cache performance."""

    def test_meta_read_with_cache(self, backend: CASLocalBackend) -> None:
        """Benchmark: repeated _read_meta calls should mostly hit cache."""
        content = b"benchmark content"
        result = backend.write_content(content)
        h = result.content_id

        # Warm up cache
        backend._read_meta(h)

        # Repeated reads should be cache hits
        for _ in range(1000):
            backend._read_meta(h)

        stats = backend.cache_stats
        assert stats["hits"] >= 1000
        assert stats["size"] >= 1

    def test_many_hashes_cache_performance(self, backend: CASLocalBackend) -> None:
        """Benchmark: cache performance with many different hashes."""
        hashes = []
        for i in range(100):
            result = backend.write_content(f"content-{i}".encode())
            hashes.append(result.content_id)

        # Read each hash multiple times — should get cache hits
        for _ in range(10):
            for h in hashes:
                backend._read_meta(h)

        stats = backend.cache_stats
        # 100 initial writes (each does read+write meta = 1 miss + cache populate)
        # 1000 subsequent reads should mostly be hits
        assert stats["hits"] >= 900
