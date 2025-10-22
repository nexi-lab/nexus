"""Multi-agent concurrency benchmarks.

These benchmarks test how well Nexus handles concurrent access patterns
typical in multi-agent AI systems.
"""

import concurrent.futures
from typing import Any

import pytest

from tests.benchmarks.conftest import BackendAdapter


@pytest.mark.benchmark(group="concurrency")
class TestConcurrentWrites:
    """Concurrent write benchmarks."""

    def test_concurrent_writes_10_agents(
        self, benchmark: pytest.fixture, backend_adapter: BackendAdapter
    ) -> None:
        """10 agents writing simultaneously to different files."""

        def concurrent_workload() -> list[Any]:
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                futures = [
                    executor.submit(backend_adapter.write, f"/agent_{i}.txt", f"data_{i}".encode())
                    for i in range(10)
                ]
                return [f.result() for f in concurrent.futures.as_completed(futures)]

        benchmark(concurrent_workload)

    def test_concurrent_writes_50_agents(
        self, benchmark: pytest.fixture, backend_adapter: BackendAdapter
    ) -> None:
        """50 agents writing simultaneously (stress test)."""

        def concurrent_workload() -> list[Any]:
            with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
                futures = [
                    executor.submit(
                        backend_adapter.write,
                        f"/agent_{i}.txt",
                        b"x" * 10_000,  # 10KB per agent
                    )
                    for i in range(50)
                ]
                return [f.result() for f in concurrent.futures.as_completed(futures)]

        benchmark(concurrent_workload)

    def test_concurrent_writes_same_file(
        self, benchmark: pytest.fixture, backend_adapter: BackendAdapter
    ) -> None:
        """Multiple agents writing to same file (tests locking/contention)."""

        def concurrent_workload() -> list[Any]:
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                futures = [
                    executor.submit(
                        backend_adapter.write,
                        "/shared_file.txt",
                        f"agent_{i}_data".encode(),
                    )
                    for i in range(10)
                ]
                return [f.result() for f in concurrent.futures.as_completed(futures)]

        benchmark(concurrent_workload)


@pytest.mark.benchmark(group="concurrency")
class TestConcurrentReads:
    """Concurrent read benchmarks."""

    def test_concurrent_reads_10_agents(
        self, benchmark: pytest.fixture, backend_adapter: BackendAdapter
    ) -> None:
        """10 agents reading same file simultaneously."""
        # Setup: write test file
        backend_adapter.write("/shared.bin", b"x" * 1_000_000)  # 1MB

        def concurrent_reads() -> list[bytes]:
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(backend_adapter.read, "/shared.bin") for _ in range(10)]
                return [f.result() for f in concurrent.futures.as_completed(futures)]

        results = benchmark(concurrent_reads)
        assert all(r == b"x" * 1_000_000 for r in results)

    def test_concurrent_reads_different_files(
        self, benchmark: pytest.fixture, backend_adapter: BackendAdapter
    ) -> None:
        """10 agents reading different files simultaneously."""
        # Setup: write 10 files
        for i in range(10):
            backend_adapter.write(f"/file_{i}.bin", b"x" * 100_000)

        def concurrent_reads() -> list[bytes]:
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                futures = [
                    executor.submit(backend_adapter.read, f"/file_{i}.bin") for i in range(10)
                ]
                return [f.result() for f in concurrent.futures.as_completed(futures)]

        results = benchmark(concurrent_reads)
        assert all(r == b"x" * 100_000 for r in results)

    def test_concurrent_reads_50_agents(
        self, benchmark: pytest.fixture, backend_adapter: BackendAdapter
    ) -> None:
        """50 agents reading simultaneously (stress test)."""
        # Setup: write test file
        backend_adapter.write("/shared_stress.bin", b"x" * 500_000)  # 500KB

        def concurrent_reads() -> list[bytes]:
            with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
                futures = [
                    executor.submit(backend_adapter.read, "/shared_stress.bin") for _ in range(50)
                ]
                return [f.result() for f in concurrent.futures.as_completed(futures)]

        results = benchmark(concurrent_reads)
        assert all(r == b"x" * 500_000 for r in results)


@pytest.mark.benchmark(group="concurrency")
class TestMixedConcurrentOperations:
    """Mixed read/write concurrency benchmarks."""

    def test_mixed_read_write_10_agents(
        self, benchmark: pytest.fixture, backend_adapter: BackendAdapter
    ) -> None:
        """5 agents reading + 5 agents writing simultaneously."""
        # Setup: write initial files for reading
        for i in range(5):
            backend_adapter.write(f"/read_{i}.bin", b"x" * 50_000)

        def mixed_workload() -> tuple[list[Any], list[bytes]]:
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                # Submit write operations
                write_futures = [
                    executor.submit(backend_adapter.write, f"/write_{i}.bin", b"y" * 50_000)
                    for i in range(5)
                ]

                # Submit read operations
                read_futures = [
                    executor.submit(backend_adapter.read, f"/read_{i}.bin") for i in range(5)
                ]

                writes = [f.result() for f in concurrent.futures.as_completed(write_futures)]
                reads = [f.result() for f in concurrent.futures.as_completed(read_futures)]

                return writes, reads

        benchmark(mixed_workload)

    def test_read_while_write_same_file(
        self, benchmark: pytest.fixture, backend_adapter: BackendAdapter
    ) -> None:
        """Test reading file while it's being written (tests consistency)."""
        # Setup: write initial version
        backend_adapter.write("/concurrent_rw.txt", b"initial")

        def concurrent_rw() -> tuple[list[Any], list[bytes]]:
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                # 10 writers
                write_futures = [
                    executor.submit(
                        backend_adapter.write,
                        "/concurrent_rw.txt",
                        f"version_{i}".encode(),
                    )
                    for i in range(10)
                ]

                # 10 readers
                read_futures = [
                    executor.submit(backend_adapter.read, "/concurrent_rw.txt") for _ in range(10)
                ]

                writes = [f.result() for f in concurrent.futures.as_completed(write_futures)]
                reads = [f.result() for f in concurrent.futures.as_completed(read_futures)]

                return writes, reads

        benchmark(concurrent_rw)

    def test_concurrent_metadata_operations(
        self, benchmark: pytest.fixture, backend_adapter: BackendAdapter
    ) -> None:
        """Test concurrent metadata operations (exists, list, etc.)."""
        # Setup: create some files
        for i in range(20):
            backend_adapter.write(f"/meta_{i}.txt", b"test")

        def concurrent_metadata() -> list[bool]:
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                futures = [
                    executor.submit(backend_adapter.exists, f"/meta_{i}.txt") for i in range(20)
                ]
                return [f.result() for f in concurrent.futures.as_completed(futures)]

        results = benchmark(concurrent_metadata)
        assert all(results), "All files should exist"
