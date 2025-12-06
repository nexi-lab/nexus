"""Benchmark tests for core Nexus operations.

Run with: pytest tests/benchmarks/ -v --benchmark-only --benchmark-group-by=func

These benchmarks identify Python hotspots for potential Rust acceleration.
See issue #570 for context.
"""

from __future__ import annotations

import hashlib

import pytest

# =============================================================================
# FILE OPERATIONS BENCHMARKS
# =============================================================================


@pytest.mark.benchmark_file_ops
class TestFileOperationBenchmarks:
    """Benchmarks for file read/write operations."""

    def test_write_tiny_file(self, benchmark, benchmark_nexus, sample_files):
        """Benchmark writing a tiny file (13 bytes)."""
        nx = benchmark_nexus
        content = sample_files["tiny"]
        counter = [0]

        def write_file():
            counter[0] += 1
            nx.write(f"/bench_tiny_{counter[0]}.txt", content)

        benchmark(write_file)

    def test_write_small_file(self, benchmark, benchmark_nexus, sample_files):
        """Benchmark writing a small file (1 KB)."""
        nx = benchmark_nexus
        content = sample_files["small"]
        counter = [0]

        def write_file():
            counter[0] += 1
            nx.write(f"/bench_small_{counter[0]}.txt", content)

        benchmark(write_file)

    def test_write_medium_file(self, benchmark, benchmark_nexus, sample_files):
        """Benchmark writing a medium file (64 KB)."""
        nx = benchmark_nexus
        content = sample_files["medium"]
        counter = [0]

        def write_file():
            counter[0] += 1
            nx.write(f"/bench_medium_{counter[0]}.txt", content)

        benchmark(write_file)

    def test_write_large_file(self, benchmark, benchmark_nexus, sample_files):
        """Benchmark writing a large file (1 MB)."""
        nx = benchmark_nexus
        content = sample_files["large"]
        counter = [0]

        def write_file():
            counter[0] += 1
            nx.write(f"/bench_large_{counter[0]}.txt", content)

        benchmark(write_file)

    def test_read_tiny_file(self, benchmark, populated_nexus):
        """Benchmark reading a tiny file (13 bytes)."""
        nx = populated_nexus

        def read_file():
            return nx.read("/test_tiny.bin")

        result = benchmark(read_file)
        assert len(result) == 13

    def test_read_small_file(self, benchmark, populated_nexus):
        """Benchmark reading a small file (1 KB)."""
        nx = populated_nexus

        def read_file():
            return nx.read("/test_small.bin")

        result = benchmark(read_file)
        assert len(result) == 1024

    def test_read_medium_file(self, benchmark, populated_nexus):
        """Benchmark reading a medium file (64 KB)."""
        nx = populated_nexus

        def read_file():
            return nx.read("/test_medium.bin")

        result = benchmark(read_file)
        assert len(result) == 64 * 1024

    def test_read_large_file(self, benchmark, populated_nexus):
        """Benchmark reading a large file (1 MB)."""
        nx = populated_nexus

        def read_file():
            return nx.read("/test_large.bin")

        result = benchmark(read_file)
        assert len(result) == 1024 * 1024

    def test_read_cached_file(self, benchmark, populated_nexus):
        """Benchmark reading a file that's already in cache."""
        nx = populated_nexus
        # Pre-warm cache
        nx.read("/test_small.bin")
        nx.read("/test_small.bin")

        def read_file():
            return nx.read("/test_small.bin")

        result = benchmark(read_file)
        assert len(result) == 1024

    def test_exists_check(self, benchmark, populated_nexus):
        """Benchmark file existence check."""
        nx = populated_nexus

        def check_exists():
            return nx.exists("/test_small.bin")

        result = benchmark(check_exists)
        assert result is True

    def test_exists_check_nonexistent(self, benchmark, populated_nexus):
        """Benchmark existence check for nonexistent file."""
        nx = populated_nexus

        def check_exists():
            return nx.exists("/nonexistent_file.txt")

        result = benchmark(check_exists)
        assert result is False

    def test_delete_file(self, benchmark, benchmark_nexus, sample_files):
        """Benchmark file deletion."""
        nx = benchmark_nexus
        content = sample_files["small"]
        counter = [0]

        def delete_file():
            counter[0] += 1
            path = f"/delete_bench_{counter[0]}.txt"
            nx.write(path, content)
            nx.delete(path)

        benchmark(delete_file)


# =============================================================================
# DIRECTORY AND GLOB BENCHMARKS
# =============================================================================


@pytest.mark.benchmark_glob
class TestGlobBenchmarks:
    """Benchmarks for directory listing and glob operations."""

    def test_list_small_directory(self, benchmark, populated_nexus):
        """Benchmark listing a directory with ~10 items."""
        nx = populated_nexus

        def list_dir():
            return nx.list("/dir_0")

        result = benchmark(list_dir)
        assert len(result) > 0

    def test_list_large_directory(self, benchmark, populated_nexus):
        """Benchmark listing a directory with ~300 items."""
        nx = populated_nexus

        def list_dir():
            return nx.list("/many_files")

        result = benchmark(list_dir)
        assert len(result) >= 100

    def test_list_recursive(self, benchmark, populated_nexus):
        """Benchmark recursive directory listing."""
        nx = populated_nexus

        def list_recursive():
            return nx.list("/", recursive=True)

        result = benchmark(list_recursive)
        assert len(result) > 100

    def test_glob_simple_pattern(self, benchmark, populated_nexus):
        """Benchmark simple glob pattern (*.txt)."""
        nx = populated_nexus

        def glob_files():
            return nx.glob("*.txt", "/many_files")

        result = benchmark(glob_files)
        assert len(result) >= 100

    def test_glob_extension_pattern(self, benchmark, populated_nexus):
        """Benchmark glob with multiple extensions."""
        nx = populated_nexus

        def glob_files():
            # Find all .py and .json files
            py_files = nx.glob("*.py", "/many_files")
            json_files = nx.glob("*.json", "/many_files")
            return py_files + json_files

        result = benchmark(glob_files)
        assert len(result) >= 200

    def test_glob_recursive_pattern(self, benchmark, populated_nexus):
        """Benchmark recursive glob pattern (**/*)."""
        nx = populated_nexus

        def glob_files():
            return nx.glob("**/*.bin", "/")

        result = benchmark(glob_files)
        assert len(result) > 0

    def test_glob_deep_path(self, benchmark, deep_directory_nexus):
        """Benchmark glob in deep directory structure."""
        nx = deep_directory_nexus

        def glob_files():
            return nx.glob("*.txt", "/level_0/level_1/level_2/level_3/level_4")

        benchmark(glob_files)


# =============================================================================
# CONTENT HASHING BENCHMARKS
# =============================================================================


@pytest.mark.benchmark_hash
class TestHashingBenchmarks:
    """Benchmarks for content hashing (CAS operations).

    These benchmarks compare Python hashlib vs potential Rust BLAKE3.
    """

    def test_sha256_tiny(self, benchmark, sample_files):
        """Benchmark SHA256 hashing of tiny content (13 bytes)."""
        content = sample_files["tiny"]

        def hash_content():
            return hashlib.sha256(content).hexdigest()

        result = benchmark(hash_content)
        assert len(result) == 64

    def test_sha256_small(self, benchmark, sample_files):
        """Benchmark SHA256 hashing of small content (1 KB)."""
        content = sample_files["small"]

        def hash_content():
            return hashlib.sha256(content).hexdigest()

        result = benchmark(hash_content)
        assert len(result) == 64

    def test_sha256_medium(self, benchmark, sample_files):
        """Benchmark SHA256 hashing of medium content (64 KB)."""
        content = sample_files["medium"]

        def hash_content():
            return hashlib.sha256(content).hexdigest()

        result = benchmark(hash_content)
        assert len(result) == 64

    def test_sha256_large(self, benchmark, sample_files):
        """Benchmark SHA256 hashing of large content (1 MB)."""
        content = sample_files["large"]

        def hash_content():
            return hashlib.sha256(content).hexdigest()

        result = benchmark(hash_content)
        assert len(result) == 64

    def test_sha256_xlarge(self, benchmark, sample_files):
        """Benchmark SHA256 hashing of xlarge content (10 MB)."""
        content = sample_files["xlarge"]

        def hash_content():
            return hashlib.sha256(content).hexdigest()

        result = benchmark(hash_content)
        assert len(result) == 64

    def test_md5_medium(self, benchmark, sample_files):
        """Benchmark MD5 hashing of medium content (64 KB) - for comparison."""
        content = sample_files["medium"]

        def hash_content():
            return hashlib.md5(content).hexdigest()

        result = benchmark(hash_content)
        assert len(result) == 32

    def test_sha256_incremental(self, benchmark, sample_files):
        """Benchmark incremental SHA256 hashing (simulating streaming)."""
        content = sample_files["large"]
        chunk_size = 64 * 1024  # 64 KB chunks

        def hash_incremental():
            hasher = hashlib.sha256()
            for i in range(0, len(content), chunk_size):
                hasher.update(content[i : i + chunk_size])
            return hasher.hexdigest()

        result = benchmark(hash_incremental)
        assert len(result) == 64


# =============================================================================
# METADATA QUERY BENCHMARKS
# =============================================================================


@pytest.mark.benchmark_metadata
class TestMetadataBenchmarks:
    """Benchmarks for metadata operations."""

    def test_get_metadata_single(self, benchmark, populated_nexus):
        """Benchmark getting metadata for a single file."""
        nx = populated_nexus

        def get_meta():
            return nx.metadata.get("/test_small.bin")

        result = benchmark(get_meta)
        assert result is not None

    def test_get_metadata_nonexistent(self, benchmark, populated_nexus):
        """Benchmark getting metadata for nonexistent file."""
        nx = populated_nexus

        def get_meta():
            return nx.metadata.get("/nonexistent.txt")

        result = benchmark(get_meta)
        assert result is None

    def test_list_metadata_small(self, benchmark, populated_nexus):
        """Benchmark listing metadata for small directory."""
        nx = populated_nexus

        def list_meta():
            return nx.metadata.list("/dir_0/")

        benchmark(list_meta)

    def test_list_metadata_large(self, benchmark, populated_nexus):
        """Benchmark listing metadata for large directory."""
        nx = populated_nexus

        def list_meta():
            return nx.metadata.list("/many_files/")

        benchmark(list_meta)

    def test_exists_metadata_cached(self, benchmark, populated_nexus):
        """Benchmark cached metadata existence check."""
        nx = populated_nexus
        # Pre-warm cache
        nx.metadata.get("/test_small.bin")

        def exists_meta():
            return nx.metadata.exists("/test_small.bin")

        result = benchmark(exists_meta)
        assert result is True

    def test_set_file_metadata(self, benchmark, populated_nexus):
        """Benchmark setting file metadata key-value."""
        nx = populated_nexus
        counter = [0]

        def set_meta():
            counter[0] += 1
            nx.metadata.set_file_metadata(
                "/test_small.bin", f"key_{counter[0]}", f"value_{counter[0]}"
            )

        benchmark(set_meta)

    def test_get_file_metadata(self, benchmark, populated_nexus):
        """Benchmark getting file metadata key-value."""
        nx = populated_nexus
        nx.metadata.set_file_metadata("/test_small.bin", "bench_key", "bench_value")

        def get_meta():
            return nx.metadata.get_file_metadata("/test_small.bin", "bench_key")

        result = benchmark(get_meta)
        assert result == "bench_value"


# =============================================================================
# PERMISSION CHECK BENCHMARKS
# =============================================================================


@pytest.mark.benchmark_permissions
class TestPermissionBenchmarks:
    """Benchmarks for permission checking (ReBAC).

    These benchmarks test both Python and Rust implementations.
    """

    def test_permission_check_simple(self, benchmark, benchmark_nexus_with_permissions):
        """Benchmark simple permission check (no tuples needed)."""
        nx = benchmark_nexus_with_permissions

        # Set up a simple permission using rebac_write
        if hasattr(nx, "_rebac_manager") and nx._rebac_manager:
            nx._rebac_manager.rebac_write(
                subject=("agent", "benchmark_agent"),
                relation="direct_viewer",
                object=("file", "/test_permission.txt"),
            )

        def check_perm():
            # This will go through permission checking
            return nx.exists("/test_permission.txt")

        benchmark(check_perm)

    def test_permission_check_bulk_python(self, benchmark, benchmark_nexus):
        """Benchmark bulk permission checking in Python."""
        # Import the Python implementation
        from nexus.core.rebac_fast import _check_permissions_bulk_python

        # Create test data
        checks = [(("user", f"user_{i}"), "read", ("file", f"/file_{i}.txt")) for i in range(100)]

        tuples = [
            {
                "subject_type": "user",
                "subject_id": f"user_{i}",
                "subject_relation": None,
                "relation": "read",
                "object_type": "file",
                "object_id": f"/file_{i}.txt",
            }
            for i in range(100)
        ]

        namespace_configs = {
            "file": {
                "relations": {"read": "direct", "write": "direct"},
                "permissions": {"view": ["read"], "edit": ["write"]},
            }
        }

        def check_bulk():
            return _check_permissions_bulk_python(checks, tuples, namespace_configs)

        result = benchmark(check_bulk)
        assert len(result) == 100

    def test_permission_check_bulk_rust(self, benchmark, benchmark_nexus):
        """Benchmark bulk permission checking in Rust (if available)."""
        from nexus.core.rebac_fast import RUST_AVAILABLE, check_permissions_bulk_with_fallback

        # Create test data
        checks = [(("user", f"user_{i}"), "read", ("file", f"/file_{i}.txt")) for i in range(100)]

        tuples = [
            {
                "subject_type": "user",
                "subject_id": f"user_{i}",
                "subject_relation": None,
                "relation": "read",
                "object_type": "file",
                "object_id": f"/file_{i}.txt",
            }
            for i in range(100)
        ]

        namespace_configs = {
            "file": {
                "relations": {"read": "direct", "write": "direct"},
                "permissions": {"view": ["read"], "edit": ["write"]},
            }
        }

        def check_bulk():
            return check_permissions_bulk_with_fallback(
                checks, tuples, namespace_configs, force_python=False
            )

        result = benchmark(check_bulk)
        assert len(result) == 100

        # Print whether Rust was used
        if RUST_AVAILABLE:
            print("\n[INFO] Rust acceleration was used for this benchmark")
        else:
            print("\n[INFO] Python fallback was used (Rust not available)")

    def test_permission_check_scale_1000(self, benchmark, benchmark_nexus):
        """Benchmark 1000 permission checks."""
        from nexus.core.rebac_fast import check_permissions_bulk_with_fallback

        checks = [(("user", f"user_{i}"), "read", ("file", f"/file_{i}.txt")) for i in range(1000)]

        tuples = [
            {
                "subject_type": "user",
                "subject_id": f"user_{i}",
                "subject_relation": None,
                "relation": "read",
                "object_type": "file",
                "object_id": f"/file_{i}.txt",
            }
            for i in range(1000)
        ]

        namespace_configs = {
            "file": {
                "relations": {"read": "direct"},
                "permissions": {},
            }
        }

        def check_bulk():
            return check_permissions_bulk_with_fallback(checks, tuples, namespace_configs)

        result = benchmark(check_bulk)
        assert len(result) == 1000


# =============================================================================
# PATH RESOLUTION BENCHMARKS
# =============================================================================


@pytest.mark.benchmark_file_ops
class TestPathResolutionBenchmarks:
    """Benchmarks for path resolution and validation."""

    def test_path_validation_simple(self, benchmark, benchmark_nexus):
        """Benchmark simple path validation."""
        nx = benchmark_nexus

        def validate():
            return nx._validate_path("/simple/path/to/file.txt")

        result = benchmark(validate)
        assert result == "/simple/path/to/file.txt"

    def test_path_validation_deep(self, benchmark, benchmark_nexus):
        """Benchmark deep path validation."""
        nx = benchmark_nexus
        deep_path = "/" + "/".join([f"level_{i}" for i in range(20)]) + "/file.txt"

        def validate():
            return nx._validate_path(deep_path)

        result = benchmark(validate)
        assert "level_19" in result

    def test_path_resolution_deep(self, benchmark, benchmark_nexus):
        """Benchmark path resolution with deep paths."""
        nx = benchmark_nexus

        def validate():
            # Use a valid deep path (no .. or . segments - those are rejected for security)
            return nx._validate_path("/foo/bar/baz/qux/deep/nested/file.txt")

        benchmark(validate)


# =============================================================================
# BULK OPERATIONS BENCHMARKS
# =============================================================================


@pytest.mark.benchmark_file_ops
class TestBulkOperationBenchmarks:
    """Benchmarks for bulk operations."""

    def test_write_batch_10(self, benchmark, benchmark_nexus, sample_files):
        """Benchmark writing 10 files in a batch."""
        nx = benchmark_nexus
        content = sample_files["small"]
        counter = [0]

        def write_batch():
            counter[0] += 1
            batch = [(f"/batch_{counter[0]}/file_{i}.txt", content) for i in range(10)]
            nx.write_batch(batch)

        benchmark(write_batch)

    def test_write_batch_100(self, benchmark, benchmark_nexus, sample_files):
        """Benchmark writing 100 files in a batch."""
        nx = benchmark_nexus
        content = sample_files["tiny"]
        counter = [0]

        def write_batch():
            counter[0] += 1
            batch = [(f"/batch100_{counter[0]}/file_{i}.txt", content) for i in range(100)]
            nx.write_batch(batch)

        benchmark(write_batch)

    def test_read_bulk_10(self, benchmark, populated_nexus):
        """Benchmark reading 10 files in bulk."""
        nx = populated_nexus
        paths = [f"/many_files/file_{i:04d}.txt" for i in range(10)]

        def read_bulk():
            return nx.read_bulk(paths)

        result = benchmark(read_bulk)
        assert len(result) == 10

    def test_read_bulk_50(self, benchmark, populated_nexus):
        """Benchmark reading 50 files in bulk."""
        nx = populated_nexus
        paths = [f"/many_files/file_{i:04d}.txt" for i in range(50)]

        def read_bulk():
            return nx.read_bulk(paths)

        result = benchmark(read_bulk)
        assert len(result) == 50


# =============================================================================
# BLAKE3 HASHING BENCHMARKS (Rust-accelerated)
# =============================================================================


@pytest.mark.benchmark_hash
class TestBlake3HashingBenchmarks:
    """Benchmarks for BLAKE3 content hashing (Rust-accelerated).

    These benchmarks compare the performance of BLAKE3 (Rust-accelerated)
    against the SHA-256 fallback for content-addressable storage.

    See issue #571 for context.
    """

    def test_hash_tiny_content(self, benchmark):
        """Benchmark hashing tiny content (13 bytes)."""
        from nexus.core.hash_fast import hash_content

        content = b"Hello, World!"

        result = benchmark(hash_content, content)
        assert len(result) == 64  # 256-bit hash = 64 hex chars

    def test_hash_1kb_content(self, benchmark):
        """Benchmark hashing 1 KB content."""
        from nexus.core.hash_fast import hash_content

        content = b"x" * 1024

        result = benchmark(hash_content, content)
        assert len(result) == 64

    def test_hash_64kb_content(self, benchmark):
        """Benchmark hashing 64 KB content."""
        from nexus.core.hash_fast import hash_content

        content = b"x" * (64 * 1024)

        result = benchmark(hash_content, content)
        assert len(result) == 64

    def test_hash_1mb_content(self, benchmark):
        """Benchmark hashing 1 MB content."""
        from nexus.core.hash_fast import hash_content

        content = b"x" * (1024 * 1024)

        result = benchmark(hash_content, content)
        assert len(result) == 64

    def test_hash_10mb_content(self, benchmark):
        """Benchmark hashing 10 MB content."""
        from nexus.core.hash_fast import hash_content

        content = b"x" * (10 * 1024 * 1024)

        result = benchmark(hash_content, content)
        assert len(result) == 64

    def test_hash_smart_256kb_content(self, benchmark):
        """Benchmark smart hashing 256 KB content (threshold)."""
        from nexus.core.hash_fast import hash_content_smart

        content = b"x" * (256 * 1024)

        result = benchmark(hash_content_smart, content)
        assert len(result) == 64

    def test_hash_smart_1mb_content(self, benchmark):
        """Benchmark smart hashing 1 MB content (uses sampling)."""
        from nexus.core.hash_fast import hash_content_smart

        content = b"x" * (1024 * 1024)

        result = benchmark(hash_content_smart, content)
        assert len(result) == 64

    def test_hash_smart_10mb_content(self, benchmark):
        """Benchmark smart hashing 10 MB content (uses sampling)."""
        from nexus.core.hash_fast import hash_content_smart

        content = b"x" * (10 * 1024 * 1024)

        result = benchmark(hash_content_smart, content)
        assert len(result) == 64

    def test_sha256_baseline_1mb(self, benchmark):
        """Baseline: SHA-256 hashing 1 MB content."""
        content = b"x" * (1024 * 1024)

        def sha256_hash():
            return hashlib.sha256(content).hexdigest()

        result = benchmark(sha256_hash)
        assert len(result) == 64

    def test_sha256_baseline_10mb(self, benchmark):
        """Baseline: SHA-256 hashing 10 MB content."""
        content = b"x" * (10 * 1024 * 1024)

        def sha256_hash():
            return hashlib.sha256(content).hexdigest()

        result = benchmark(sha256_hash)
        assert len(result) == 64

    def test_rust_availability(self):
        """Check if Rust acceleration is available."""
        from nexus.core.hash_fast import is_rust_available

        available = is_rust_available()
        print(f"\n[INFO] Rust BLAKE3 acceleration: {'AVAILABLE' if available else 'NOT AVAILABLE'}")
        # This test always passes - just informational
