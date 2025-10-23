"""Cache effectiveness benchmarks.

These benchmarks measure cache hit rates and performance improvements
from caching in Nexus filesystem.
"""

import pytest

from tests.benchmarks.conftest import BackendAdapter


@pytest.mark.benchmark(group="cache")
class TestCacheEffectiveness:
    """Cache performance benchmarks."""

    def test_cold_read(self, benchmark: pytest.fixture, backend_adapter: BackendAdapter) -> None:
        """Benchmark first read of a file (cold cache)."""
        data = b"x" * (1024 * 1024)  # 1MB
        backend_adapter.write("/cold_cache.bin", data)

        # Clear any OS-level caches if possible (this is a cold read)
        def cold_read() -> bytes:
            return backend_adapter.read("/cold_cache.bin")

        result = benchmark.pedantic(cold_read, iterations=1, rounds=10)
        assert result == data

    def test_warm_read(self, benchmark: pytest.fixture, backend_adapter: BackendAdapter) -> None:
        """Benchmark repeated reads of same file (warm cache)."""
        data = b"x" * (1024 * 1024)  # 1MB
        backend_adapter.write("/warm_cache.bin", data)

        # Warm up cache
        backend_adapter.read("/warm_cache.bin")

        # Benchmark cached reads
        def warm_read() -> bytes:
            return backend_adapter.read("/warm_cache.bin")

        result = benchmark(warm_read)
        assert result == data

    def test_cache_read_pattern(
        self, benchmark: pytest.fixture, backend_adapter: BackendAdapter
    ) -> None:
        """Benchmark reading multiple files with cache.

        Tests cache effectiveness when accessing a working set of files.
        """
        # Setup: create 10 files
        files = []
        for i in range(10):
            path = f"/cache_file_{i}.bin"
            data = b"x" * 100_000 + str(i).encode()  # 100KB + unique suffix
            backend_adapter.write(path, data)
            files.append(path)

        # Warm up cache by reading all files once
        for path in files:
            backend_adapter.read(path)

        # Benchmark reading files in round-robin pattern (simulates AI agent access)
        def read_pattern() -> None:
            for _ in range(5):  # 5 passes through all files
                for path in files:
                    backend_adapter.read(path)

        benchmark(read_pattern)

    def test_small_file_cache(
        self, benchmark: pytest.fixture, backend_adapter: BackendAdapter
    ) -> None:
        """Benchmark cache effectiveness for small files (metadata-heavy)."""
        # Setup: 50 small files (1KB each)
        for i in range(50):
            backend_adapter.write(f"/small_{i}.txt", b"x" * 1024)

        # Warm up cache
        for i in range(50):
            backend_adapter.read(f"/small_{i}.txt")

        # Benchmark cached small file reads
        def read_small_files() -> None:
            for i in range(50):
                backend_adapter.read(f"/small_{i}.txt")

        benchmark(read_small_files)

    def test_large_file_cache(
        self, benchmark: pytest.fixture, backend_adapter: BackendAdapter
    ) -> None:
        """Benchmark cache effectiveness for large files."""
        data = b"x" * (10 * 1024 * 1024)  # 10MB
        backend_adapter.write("/large_cache.bin", data)

        # Warm up cache
        backend_adapter.read("/large_cache.bin")

        # Benchmark cached large file reads
        def read_large() -> bytes:
            return backend_adapter.read("/large_cache.bin")

        result = benchmark(read_large)
        assert result == data

    def test_mixed_cache_hit_miss(
        self, benchmark: pytest.fixture, backend_adapter: BackendAdapter
    ) -> None:
        """Benchmark mixed cache hits and misses.

        Simulates realistic workload with some cached and some new files.
        """
        # Setup: create 20 files, only cache half of them
        for i in range(20):
            backend_adapter.write(f"/mixed_{i}.bin", b"x" * 50_000)

        # Warm up cache for first 10 files
        for i in range(10):
            backend_adapter.read(f"/mixed_{i}.bin")

        # Benchmark: read all 20 files (10 cached, 10 cold)
        def mixed_reads() -> None:
            for i in range(20):
                backend_adapter.read(f"/mixed_{i}.bin")

        benchmark(mixed_reads)
