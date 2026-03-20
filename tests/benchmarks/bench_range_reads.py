"""Benchmarks for range read optimizations (Issue #2940).

Measures the performance of read_content_range vs full read_content + slice,
for both single-blob and CDC-chunked content.

Usage:
    uv run pytest tests/benchmarks/bench_range_reads.py -v -o "addopts="
"""

import os
from pathlib import Path

import pytest

from nexus.backends.engines.cdc import CDC_THRESHOLD_BYTES
from nexus.backends.storage.cas_local import CASLocalBackend


@pytest.fixture
def backend(tmp_path: Path) -> CASLocalBackend:
    return CASLocalBackend(root_path=tmp_path / "range_bench")


class TestRangeReadBenchmark:
    """Benchmark range read performance."""

    def test_range_read_small_file_equivalent(self, backend: CASLocalBackend) -> None:
        """Verify range read returns correct data for small files."""
        content = os.urandom(64 * 1024)  # 64KB
        h = backend.write_content(content).content_id

        # Full read + slice
        full = backend.read_content(h)
        expected = full[1000:2000]

        # Range read
        actual = backend.read_content_range(h, 1000, 2000)

        assert actual == expected

    def test_range_read_chunked_file_equivalent(self, backend: CASLocalBackend) -> None:
        """Verify range read returns correct data for chunked files."""
        content = os.urandom(CDC_THRESHOLD_BYTES + 1024 * 1024)
        h = backend.write_content(content).content_id

        # Full read + slice
        start = CDC_THRESHOLD_BYTES // 2
        end = start + 8192
        expected = content[start:end]

        # Range read
        actual = backend.read_content_range(h, start, end)

        assert actual == expected

    def test_range_read_first_chunk_only(self, backend: CASLocalBackend) -> None:
        """Benchmark: reading first 4KB of a large chunked file."""
        content = os.urandom(CDC_THRESHOLD_BYTES + 2 * 1024 * 1024)
        h = backend.write_content(content).content_id

        result = backend.read_content_range(h, 0, 4096)
        assert result == content[:4096]

    def test_range_read_last_chunk_only(self, backend: CASLocalBackend) -> None:
        """Benchmark: reading last 4KB of a large chunked file."""
        content = os.urandom(CDC_THRESHOLD_BYTES + 2 * 1024 * 1024)
        h = backend.write_content(content).content_id

        end = len(content)
        start = end - 4096
        result = backend.read_content_range(h, start, end)
        assert result == content[start:end]
