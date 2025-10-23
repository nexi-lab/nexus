"""CAS deduplication efficiency benchmarks.

These benchmarks measure how efficiently Nexus deduplicates content using
content-addressable storage (CAS) compared to traditional filesystem storage.
"""

from pathlib import Path

import pytest

from nexus import NexusFS
from tests.benchmarks.conftest import BackendAdapter


@pytest.mark.benchmark(group="deduplication")
class TestDeduplicationEfficiency:
    """CAS deduplication benchmarks."""

    def test_duplicate_write_performance(
        self, benchmark: pytest.fixture, backend_adapter: BackendAdapter
    ) -> None:
        """Benchmark writing same content 100 times.

        For Nexus with CAS, subsequent writes should be fast as content is deduplicated.
        For local filesystem, each write creates a new file.
        """
        content = b"x" * 100_000  # 100KB

        def write_duplicates() -> None:
            for i in range(100):
                backend_adapter.write(f"/duplicate_{i}.bin", content)

        benchmark(write_duplicates)

    def test_deduplication_storage_savings(
        self, nexus_backend: NexusFS | Path, backend_type: str
    ) -> None:
        """Measure storage savings from deduplication (not a benchmark, just measurement).

        This test measures actual storage usage to demonstrate CAS efficiency.
        """
        content = b"x" * 100_000  # 100KB
        adapter = BackendAdapter(nexus_backend)

        # Write same content 100 times with different paths
        for i in range(100):
            adapter.write(f"/dedup_{i}.bin", content)

        # Measure storage
        if isinstance(nexus_backend, NexusFS):
            # For Nexus, check actual CAS storage
            backend = nexus_backend.backend
            if hasattr(backend, "base_path"):
                cas_dir = backend.base_path / "cas"  # type: ignore
                if cas_dir.exists():
                    # Count unique content files in CAS
                    cas_files = list(cas_dir.glob("**/*"))
                    actual_files = [f for f in cas_files if f.is_file()]
                    # With perfect deduplication, should have only 1 unique content file
                    # (all 100 files share the same content hash)
                    print(f"\nNexus CAS: {len(actual_files)} unique content blocks for 100 files")
                    # Assert we have significant deduplication (should be close to 1)
                    assert len(actual_files) <= 5, (
                        f"Expected ~1 CAS file due to deduplication, got {len(actual_files)}"
                    )
        else:
            # For local filesystem, all files are stored separately
            fs_path: Path = nexus_backend  # type: ignore
            files = list(fs_path.glob("dedup_*.bin"))
            print(f"\nLocal FS: {len(files)} separate files for 100 files")
            assert len(files) == 100, "Local FS should have all 100 files"

    def test_mixed_content_deduplication(
        self, benchmark: pytest.fixture, backend_adapter: BackendAdapter
    ) -> None:
        """Benchmark writing files with partial deduplication.

        50 unique files, each duplicated twice = 100 total files.
        """
        base_content = b"unique_content_"

        def write_mixed() -> None:
            for i in range(50):
                content = base_content + str(i).encode() * 1000
                # Write each unique content twice
                backend_adapter.write(f"/mixed_{i}_a.bin", content)
                backend_adapter.write(f"/mixed_{i}_b.bin", content)

        benchmark(write_mixed)

    def test_incremental_write_deduplication(
        self, benchmark: pytest.fixture, backend_adapter: BackendAdapter
    ) -> None:
        """Benchmark incremental writes with same content.

        Tests how fast subsequent writes of duplicate content are.
        """
        content = b"x" * 50_000  # 50KB

        # First write
        backend_adapter.write("/base.bin", content)

        # Benchmark subsequent duplicate writes
        def write_duplicate() -> None:
            backend_adapter.write("/duplicate_copy.bin", content)

        benchmark(write_duplicate)
