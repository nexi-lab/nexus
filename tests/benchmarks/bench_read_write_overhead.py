"""Benchmarks for read_bulk and read-before-write overhead (Issue #3710).

Run: pytest tests/benchmarks/bench_read_write_overhead.py -v --benchmark-only
"""

import pytest

# =============================================================================
# READ BULK OVERHEAD
# =============================================================================


@pytest.mark.benchmark_file_ops
class TestReadBulkOverhead:
    """Benchmark read_bulk vs sequential reads for 100 files."""

    def test_read_bulk_vs_sequential(self, benchmark, populated_nexus):
        """Benchmark read_bulk on 100 files from /many_files/."""
        nx = populated_nexus
        paths = [f"/many_files/file_{i:04d}.txt" for i in range(100)]

        def read_bulk():
            return nx.read_bulk(paths)

        result = benchmark(read_bulk)
        assert len(result) == 100
        assert all(v is not None for v in result.values())


@pytest.mark.benchmark_file_ops
class TestReadBulkSequentialBaseline:
    """Sequential read baseline for comparison with read_bulk."""

    def test_sequential_read_100(self, benchmark, populated_nexus):
        """Benchmark sequential sys_read on 100 files as baseline."""
        nx = populated_nexus
        paths = [f"/many_files/file_{i:04d}.txt" for i in range(100)]

        def sequential_read():
            results = {}
            for path in paths:
                results[path] = nx.sys_read(path)
            return results

        result = benchmark(sequential_read)
        assert len(result) == 100
        assert all(v is not None for v in result.values())


# =============================================================================
# WRITE NEW-VS-EXISTING OVERHEAD
# =============================================================================


@pytest.mark.benchmark_file_ops
class TestWriteNewFile:
    """Benchmark write overhead for new (non-existing) paths."""

    def test_write_new_path(self, benchmark, benchmark_nexus):
        """Benchmark writes to unique new paths (no prior content)."""
        nx = benchmark_nexus
        counter = [0]

        def write_new():
            counter[0] += 1
            result = nx.write(f"/bench_new_{counter[0]}.txt", b"content")
            return result

        result = benchmark(write_new)
        assert "content_id" in result


@pytest.mark.benchmark_file_ops
class TestWriteExistingFile:
    """Benchmark write overhead for existing paths (overwrite)."""

    def test_write_existing_path(self, benchmark, benchmark_nexus):
        """Benchmark overwrites on a pre-created file."""
        nx = benchmark_nexus
        # Pre-create the file so every benchmark iteration is an overwrite
        nx.write("/bench_existing.txt", b"initial content")

        def write_existing():
            result = nx.write("/bench_existing.txt", b"updated content")
            return result

        result = benchmark(write_existing)
        assert "content_id" in result


# =============================================================================
# READ WITH METADATA OVERHEAD
# =============================================================================


@pytest.mark.benchmark_file_ops
class TestReadWithMetadata:
    """Benchmark read with inline metadata (combined sys_read + sys_stat)."""

    def test_read_return_metadata(self, benchmark, populated_nexus):
        """Benchmark nx.read with return_metadata=True (read + stat composed)."""
        nx = populated_nexus

        def read_with_meta():
            return nx.read("/test_small.bin", return_metadata=True)

        result = benchmark(read_with_meta)
        assert isinstance(result, dict)
        assert "content" in result
        assert len(result["content"]) == 1024
        assert "content_id" in result


@pytest.mark.benchmark_file_ops
class TestReadPlusStat:
    """Benchmark separate sys_read + sys_stat as baseline for metadata comparison."""

    def test_read_plus_stat_separate(self, benchmark, populated_nexus):
        """Benchmark sys_read then sys_stat separately (two-call baseline)."""
        nx = populated_nexus

        def read_plus_stat():
            content = nx.sys_read("/test_small.bin")
            meta = nx.sys_stat("/test_small.bin")
            return {"content": content, "content_id": meta.get("content_id") if meta else None}

        result = benchmark(read_plus_stat)
        assert isinstance(result, dict)
        assert "content" in result
        assert len(result["content"]) == 1024
        assert "content_id" in result


# =============================================================================
# ROUTING OVERHEAD
# =============================================================================


@pytest.mark.benchmark_file_ops
class TestRouteOverhead:
    """Benchmark Python router vs Rust sys_read for the same path."""

    def test_python_route_time(self, benchmark, populated_nexus):
        """Benchmark the Python router.route() call directly."""
        nx = populated_nexus

        def route_only():
            return nx.router.route("/test_small.bin")

        result = benchmark(route_only)
        assert result is not None

    def test_rust_sys_read(self, benchmark, populated_nexus):
        """Benchmark Rust-backed sys_read as routing + read baseline."""
        nx = populated_nexus

        def sys_read():
            return nx.sys_read("/test_small.bin")

        result = benchmark(sys_read)
        assert len(result) == 1024
