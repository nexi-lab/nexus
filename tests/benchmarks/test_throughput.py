"""Throughput benchmarks for Nexus filesystem.

These benchmarks measure write and read performance for various file sizes,
comparing Nexus performance against raw filesystem operations.
"""

import pytest

from tests.benchmarks.conftest import BackendAdapter


@pytest.mark.benchmark(group="write-throughput")
class TestWriteThroughput:
    """Write throughput benchmarks."""

    def test_write_1kb(self, benchmark: pytest.fixture, backend_adapter: BackendAdapter) -> None:
        """Benchmark writing 1KB files."""
        data = b"x" * 1024  # 1KB

        def write_operation() -> None:
            backend_adapter.write("/test_1kb.bin", data)

        benchmark(write_operation)

    def test_write_100kb(self, benchmark: pytest.fixture, backend_adapter: BackendAdapter) -> None:
        """Benchmark writing 100KB files."""
        data = b"x" * (100 * 1024)  # 100KB

        def write_operation() -> None:
            backend_adapter.write("/test_100kb.bin", data)

        benchmark(write_operation)

    def test_write_1mb(self, benchmark: pytest.fixture, backend_adapter: BackendAdapter) -> None:
        """Benchmark writing 1MB files."""
        data = b"x" * (1024 * 1024)  # 1MB

        def write_operation() -> None:
            backend_adapter.write("/test_1mb.bin", data)

        benchmark(write_operation)

    def test_write_10mb(self, benchmark: pytest.fixture, backend_adapter: BackendAdapter) -> None:
        """Benchmark writing 10MB files."""
        data = b"x" * (10 * 1024 * 1024)  # 10MB

        def write_operation() -> None:
            backend_adapter.write("/test_10mb.bin", data)

        benchmark(write_operation)


@pytest.mark.benchmark(group="read-throughput")
class TestReadThroughput:
    """Read throughput benchmarks."""

    def test_read_1kb(self, benchmark: pytest.fixture, backend_adapter: BackendAdapter) -> None:
        """Benchmark reading 1KB files."""
        data = b"x" * 1024  # 1KB
        backend_adapter.write("/test_1kb.bin", data)

        def read_operation() -> bytes:
            return backend_adapter.read("/test_1kb.bin")

        result = benchmark(read_operation)
        assert result == data

    def test_read_100kb(self, benchmark: pytest.fixture, backend_adapter: BackendAdapter) -> None:
        """Benchmark reading 100KB files."""
        data = b"x" * (100 * 1024)  # 100KB
        backend_adapter.write("/test_100kb.bin", data)

        def read_operation() -> bytes:
            return backend_adapter.read("/test_100kb.bin")

        result = benchmark(read_operation)
        assert result == data

    def test_read_1mb(self, benchmark: pytest.fixture, backend_adapter: BackendAdapter) -> None:
        """Benchmark reading 1MB files."""
        data = b"x" * (1024 * 1024)  # 1MB
        backend_adapter.write("/test_1mb.bin", data)

        def read_operation() -> bytes:
            return backend_adapter.read("/test_1mb.bin")

        result = benchmark(read_operation)
        assert result == data

    def test_read_10mb(self, benchmark: pytest.fixture, backend_adapter: BackendAdapter) -> None:
        """Benchmark reading 10MB files."""
        data = b"x" * (10 * 1024 * 1024)  # 10MB
        backend_adapter.write("/test_10mb.bin", data)

        def read_operation() -> bytes:
            return backend_adapter.read("/test_10mb.bin")

        result = benchmark(read_operation)
        assert result == data


@pytest.mark.benchmark(group="small-files")
class TestSmallFileThroughput:
    """Small file operation benchmarks (common in AI workloads)."""

    def test_many_small_writes(
        self, benchmark: pytest.fixture, backend_adapter: BackendAdapter
    ) -> None:
        """Benchmark writing 100 small (1KB) files."""
        data = b"x" * 1024  # 1KB

        def write_many() -> None:
            for i in range(100):
                backend_adapter.write(f"/small_{i}.txt", data)

        benchmark(write_many)

    def test_many_small_reads(
        self, benchmark: pytest.fixture, backend_adapter: BackendAdapter
    ) -> None:
        """Benchmark reading 100 small (1KB) files."""
        data = b"x" * 1024  # 1KB
        # Setup: write files
        for i in range(100):
            backend_adapter.write(f"/small_{i}.txt", data)

        def read_many() -> None:
            for i in range(100):
                backend_adapter.read(f"/small_{i}.txt")

        benchmark(read_many)


@pytest.mark.benchmark(group="metadata-ops")
class TestMetadataOperations:
    """Metadata operation benchmarks."""

    def test_exists_check(self, benchmark: pytest.fixture, backend_adapter: BackendAdapter) -> None:
        """Benchmark existence checks."""
        backend_adapter.write("/test.txt", b"test data")

        def check_exists() -> bool:
            return backend_adapter.exists("/test.txt")

        result = benchmark(check_exists)
        assert result is True

    def test_list_directory(
        self, benchmark: pytest.fixture, backend_adapter: BackendAdapter
    ) -> None:
        """Benchmark directory listing."""
        # Setup: create some files
        for i in range(50):
            backend_adapter.write(f"/file_{i}.txt", b"test")

        def list_dir() -> list[str]:
            return backend_adapter.list_dir("/")

        result = benchmark(list_dir)
        # Note: embedded nexus stores files flat in root, local_fs might show actual count
        assert len(result) > 0
