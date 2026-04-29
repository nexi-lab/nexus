"""Benchmarks for Rust-accelerated path prefix matching (Issue #1565).

Run: python -m pytest tests/benchmarks/test_prefix_performance.py -v --benchmark-only
"""

import pytest

# Check if Rust module is available
try:
    import nexus_runtime

    RUST_AVAILABLE = hasattr(nexus_runtime, "batch_prefix_check")
except ImportError:
    RUST_AVAILABLE = False


def _generate_paths(n: int) -> list[str]:
    """Generate N realistic file paths."""
    return [f"/workspace/project-{i % 50}/src/module-{i % 200}/file-{i}.rs" for i in range(n)]


def _generate_prefixes(m: int) -> list[str]:
    """Generate M directory prefixes."""
    return [f"/workspace/project-{i}" for i in range(m)]


def _python_batch_prefix_check(paths: list[str], prefixes: list[str]) -> list[bool]:
    """Pure Python implementation (same as enforcer fallback)."""
    results = []
    for prefix in prefixes:
        prefix_normalized = prefix.rstrip("/") + "/"
        prefix_exact = prefix.rstrip("/")
        found = any(p.startswith(prefix_normalized) or p == prefix_exact for p in paths)
        results.append(found)
    return results


class TestPrefixPerformance:
    """Benchmark: Python vs Rust path prefix matching."""

    @pytest.mark.benchmark(group="prefix_10k_50")
    def test_python_baseline_10k_50(self, benchmark):
        """Python startswith() loop: 10K paths x 50 prefixes."""
        paths = _generate_paths(10_000)
        prefixes = _generate_prefixes(50)
        benchmark(_python_batch_prefix_check, paths, prefixes)

    @pytest.mark.skipif(not RUST_AVAILABLE, reason="nexus_runtime not available")
    @pytest.mark.benchmark(group="prefix_10k_50")
    def test_rust_10k_50(self, benchmark):
        """Rust batch_prefix_check: 10K paths x 50 prefixes."""
        paths = _generate_paths(10_000)
        prefixes = _generate_prefixes(50)
        benchmark(nexus_runtime.batch_prefix_check, paths, prefixes)

    @pytest.mark.benchmark(group="prefix_1k_10")
    def test_python_baseline_1k_10(self, benchmark):
        """Python startswith() loop: 1K paths x 10 prefixes."""
        paths = _generate_paths(1_000)
        prefixes = _generate_prefixes(10)
        benchmark(_python_batch_prefix_check, paths, prefixes)

    @pytest.mark.skipif(not RUST_AVAILABLE, reason="nexus_runtime not available")
    @pytest.mark.benchmark(group="prefix_1k_10")
    def test_rust_1k_10(self, benchmark):
        """Rust batch_prefix_check: 1K paths x 10 prefixes."""
        paths = _generate_paths(1_000)
        prefixes = _generate_prefixes(10)
        benchmark(nexus_runtime.batch_prefix_check, paths, prefixes)

    @pytest.mark.benchmark(group="prefix_100k_100")
    def test_python_baseline_100k_100(self, benchmark):
        """Python startswith() loop: 100K paths x 100 prefixes."""
        paths = _generate_paths(100_000)
        prefixes = _generate_prefixes(100)
        benchmark(_python_batch_prefix_check, paths, prefixes)

    @pytest.mark.skipif(not RUST_AVAILABLE, reason="nexus_runtime not available")
    @pytest.mark.benchmark(group="prefix_100k_100")
    def test_rust_100k_100(self, benchmark):
        """Rust batch_prefix_check: 100K paths x 100 prefixes."""
        paths = _generate_paths(100_000)
        prefixes = _generate_prefixes(100)
        benchmark(nexus_runtime.batch_prefix_check, paths, prefixes)
