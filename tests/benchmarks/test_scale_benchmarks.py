"""Scale benchmarks for Nexus filesystem operations.

These tests measure performance at scale for:
1. Listing 50k files (flat and nested)
2. Grep operations on files with short and long content
3. Writing 1k files

Run with:
    pytest tests/benchmarks/test_scale_benchmarks.py -v --benchmark-only

For quick tests with smaller data:
    pytest tests/benchmarks/test_scale_benchmarks.py -v --benchmark-only -k "small"

See issue #XXX for context.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add benchmarks directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "benchmarks"))

from performance.generate_data import (
    ContentGenerator,
    FilenameGenerator,
    GenerationConfig,
    PerformanceDataGenerator,
)

# =============================================================================
# FIXTURES
# =============================================================================


@pytest.fixture(scope="module")
def content_generator():
    """Create a content generator for benchmarks."""
    return ContentGenerator(seed=42)


@pytest.fixture(scope="module")
def filename_generator():
    """Create a filename generator for benchmarks."""
    return FilenameGenerator(seed=42)


@pytest.fixture(scope="module")
def small_scale_data(tmp_path_factory):
    """Generate small-scale test data (1% of full) for quick benchmarks."""
    output_dir = tmp_path_factory.mktemp("small_scale")
    config = GenerationConfig(
        flat_file_count=500,
        nested_file_count=500,
        nested_depth=3,
        nested_width=5,
        short_content_lines=10,
        long_content_lines=100,
        write_file_count=100,
    )
    generator = PerformanceDataGenerator(output_dir, config)
    generator.generate_all(scale=1.0)
    return output_dir


@pytest.fixture(scope="module")
def medium_scale_data(tmp_path_factory):
    """Generate medium-scale test data (10% of full) for moderate benchmarks."""
    output_dir = tmp_path_factory.mktemp("medium_scale")
    config = GenerationConfig(
        flat_file_count=5000,
        nested_file_count=5000,
        nested_depth=5,
        nested_width=5,
        short_content_lines=10,
        long_content_lines=500,
        write_file_count=500,
    )
    generator = PerformanceDataGenerator(output_dir, config)
    generator.generate_all(scale=1.0)
    return output_dir


@pytest.fixture(scope="module")
def large_scale_data(tmp_path_factory):
    """Generate large-scale test data (50k files) for full benchmarks.

    Note: This takes significant time and disk space. Use sparingly.
    """
    output_dir = tmp_path_factory.mktemp("large_scale")
    config = GenerationConfig()  # Default: 50k files
    generator = PerformanceDataGenerator(output_dir, config)
    # Only generate flat_short for quick large-scale test
    generator.generate_flat_files_short_names()
    return output_dir


# =============================================================================
# LIST BENCHMARKS - FLAT DIRECTORIES
# =============================================================================


@pytest.mark.benchmark_glob
class TestListFlatBenchmarks:
    """Benchmarks for listing files in flat directories."""

    def test_list_flat_short_names_small(self, benchmark, small_scale_data):
        """Benchmark listing 500 files with short names (flat)."""
        flat_dir = small_scale_data / "flat_short"

        def list_files():
            return list(flat_dir.iterdir())

        result = benchmark(list_files)
        assert len(result) == 500

    def test_list_flat_long_names_small(self, benchmark, small_scale_data):
        """Benchmark listing 500 files with long names (flat)."""
        flat_dir = small_scale_data / "flat_long"

        def list_files():
            return list(flat_dir.iterdir())

        result = benchmark(list_files)
        assert len(result) == 500

    def test_list_flat_short_names_medium(self, benchmark, medium_scale_data):
        """Benchmark listing 5k files with short names (flat)."""
        flat_dir = medium_scale_data / "flat_short"

        def list_files():
            return list(flat_dir.iterdir())

        result = benchmark(list_files)
        assert len(result) == 5000

    def test_list_flat_long_names_medium(self, benchmark, medium_scale_data):
        """Benchmark listing 5k files with long names (flat)."""
        flat_dir = medium_scale_data / "flat_long"

        def list_files():
            return list(flat_dir.iterdir())

        result = benchmark(list_files)
        assert len(result) == 5000

    @pytest.mark.slow
    def test_list_flat_50k_files(self, benchmark, large_scale_data):
        """Benchmark listing 50k files with short names (flat)."""
        flat_dir = large_scale_data / "flat_short"

        def list_files():
            return list(flat_dir.iterdir())

        result = benchmark(list_files)
        assert len(result) == 50000


# =============================================================================
# LIST BENCHMARKS - NESTED DIRECTORIES
# =============================================================================


@pytest.mark.benchmark_glob
class TestListNestedBenchmarks:
    """Benchmarks for listing files in nested directories."""

    def test_list_nested_short_names_small(self, benchmark, small_scale_data):
        """Benchmark listing 500 files in nested structure with short names."""
        nested_dir = small_scale_data / "nested_short"

        def list_files():
            return list(nested_dir.rglob("*"))

        result = benchmark(list_files)
        # Count only files, not directories
        file_count = sum(1 for p in result if p.is_file())
        assert file_count >= 100  # May be less due to nesting structure

    def test_list_nested_long_names_small(self, benchmark, small_scale_data):
        """Benchmark listing 500 files in nested structure with long names."""
        nested_dir = small_scale_data / "nested_long"

        def list_files():
            return list(nested_dir.rglob("*"))

        result = benchmark(list_files)
        file_count = sum(1 for p in result if p.is_file())
        assert file_count >= 100

    def test_list_nested_short_names_medium(self, benchmark, medium_scale_data):
        """Benchmark listing 5k files in nested structure with short names."""
        nested_dir = medium_scale_data / "nested_short"

        def list_files():
            return list(nested_dir.rglob("*"))

        result = benchmark(list_files)
        file_count = sum(1 for p in result if p.is_file())
        assert file_count >= 1000

    def test_list_nested_long_names_medium(self, benchmark, medium_scale_data):
        """Benchmark listing 5k files in nested structure with long names."""
        nested_dir = medium_scale_data / "nested_long"

        def list_files():
            return list(nested_dir.rglob("*"))

        result = benchmark(list_files)
        file_count = sum(1 for p in result if p.is_file())
        assert file_count >= 1000

    def test_list_nested_glob_pattern_small(self, benchmark, small_scale_data):
        """Benchmark glob pattern matching in nested structure."""
        nested_dir = small_scale_data / "nested_short"

        def glob_files():
            return list(nested_dir.rglob("*.txt"))

        result = benchmark(glob_files)
        assert len(result) >= 10

    def test_list_nested_glob_pattern_medium(self, benchmark, medium_scale_data):
        """Benchmark glob pattern matching in nested structure (medium scale)."""
        nested_dir = medium_scale_data / "nested_short"

        def glob_files():
            return list(nested_dir.rglob("*.txt"))

        result = benchmark(glob_files)
        assert len(result) >= 100


# =============================================================================
# GREP BENCHMARKS - SHORT CONTENT
# =============================================================================


@pytest.mark.benchmark_hash
class TestGrepShortContentBenchmarks:
    """Benchmarks for grep operations on files with short content (~10 lines)."""

    def test_grep_short_content_python_small(self, benchmark, small_scale_data):
        """Benchmark Python grep on short content files (small scale)."""
        import re

        grep_dir = small_scale_data / "grep_short"
        pattern = re.compile(r"ERROR")

        def grep_files():
            matches = []
            for filepath in grep_dir.glob("*.log"):
                content = filepath.read_text()
                for i, line in enumerate(content.split("\n")):
                    if pattern.search(line):
                        matches.append((str(filepath), i + 1, line))
            return matches

        result = benchmark(grep_files)
        assert len(result) >= 10  # Expect some ERROR matches

    def test_grep_short_content_python_medium(self, benchmark, medium_scale_data):
        """Benchmark Python grep on short content files (medium scale)."""
        import re

        grep_dir = medium_scale_data / "grep_short"
        pattern = re.compile(r"ERROR")

        def grep_files():
            matches = []
            for filepath in grep_dir.glob("*.log"):
                content = filepath.read_text()
                for i, line in enumerate(content.split("\n")):
                    if pattern.search(line):
                        matches.append((str(filepath), i + 1, line))
            return matches

        result = benchmark(grep_files)
        assert len(result) >= 50


# =============================================================================
# GREP BENCHMARKS - LONG CONTENT
# =============================================================================


@pytest.mark.benchmark_hash
class TestGrepLongContentBenchmarks:
    """Benchmarks for grep operations on files with long content (~1000 lines)."""

    def test_grep_long_content_python_small(self, benchmark, small_scale_data):
        """Benchmark Python grep on long content files (small scale)."""
        import re

        grep_dir = small_scale_data / "grep_long"
        pattern = re.compile(r"ERROR")

        def grep_files():
            matches = []
            for filepath in grep_dir.glob("*.log"):
                content = filepath.read_text()
                for i, line in enumerate(content.split("\n")):
                    if pattern.search(line):
                        matches.append((str(filepath), i + 1, line))
            return matches

        result = benchmark(grep_files)
        assert len(result) >= 100  # Expect many ERROR matches in long files

    def test_grep_long_content_python_medium(self, benchmark, medium_scale_data):
        """Benchmark Python grep on long content files (medium scale)."""
        import re

        grep_dir = medium_scale_data / "grep_long"
        pattern = re.compile(r"ERROR")

        def grep_files():
            matches = []
            for filepath in grep_dir.glob("*.log"):
                content = filepath.read_text()
                for i, line in enumerate(content.split("\n")):
                    if pattern.search(line):
                        matches.append((str(filepath), i + 1, line))
            return matches

        result = benchmark(grep_files)
        assert len(result) >= 500

    def test_grep_long_content_complex_pattern(self, benchmark, medium_scale_data):
        """Benchmark grep with complex regex on long content."""
        import re

        grep_dir = medium_scale_data / "grep_long"
        # More complex pattern: match timestamps with specific format
        pattern = re.compile(r"\[ERROR\].*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}")

        def grep_files():
            matches = []
            for filepath in grep_dir.glob("*.log"):
                content = filepath.read_text()
                for i, line in enumerate(content.split("\n")):
                    if pattern.search(line):
                        matches.append((str(filepath), i + 1, line))
            return matches

        result = benchmark(grep_files)
        assert len(result) >= 100


# =============================================================================
# GREP BENCHMARKS - NESTED STRUCTURE
# =============================================================================


@pytest.mark.benchmark_hash
class TestGrepNestedBenchmarks:
    """Benchmarks for grep in nested directory structures."""

    def test_grep_nested_all_files_small(self, benchmark, small_scale_data):
        """Benchmark grep across all files in nested structure (small scale)."""
        import re

        grep_dir = small_scale_data / "grep_nested"
        pattern = re.compile(r"ERROR|TODO|raise")

        def grep_files():
            matches = []
            for filepath in grep_dir.rglob("*"):
                if filepath.is_file():
                    try:
                        content = filepath.read_text()
                        for i, line in enumerate(content.split("\n")):
                            if pattern.search(line):
                                matches.append((str(filepath), i + 1, line))
                    except UnicodeDecodeError:
                        pass
            return matches

        result = benchmark(grep_files)
        assert len(result) >= 50

    def test_grep_nested_specific_extension_small(self, benchmark, small_scale_data):
        """Benchmark grep on specific file types in nested structure."""
        import re

        grep_dir = small_scale_data / "grep_nested"
        pattern = re.compile(r"def\s+\w+")

        def grep_files():
            matches = []
            for filepath in grep_dir.rglob("*.py"):
                content = filepath.read_text()
                for i, line in enumerate(content.split("\n")):
                    if pattern.search(line):
                        matches.append((str(filepath), i + 1, line))
            return matches

        result = benchmark(grep_files)
        assert len(result) >= 20

    def test_grep_nested_all_files_medium(self, benchmark, medium_scale_data):
        """Benchmark grep across all files in nested structure (medium scale)."""
        import re

        grep_dir = medium_scale_data / "grep_nested"
        pattern = re.compile(r"ERROR")

        def grep_files():
            matches = []
            for filepath in grep_dir.rglob("*.log"):
                content = filepath.read_text()
                for i, line in enumerate(content.split("\n")):
                    if pattern.search(line):
                        matches.append((str(filepath), i + 1, line))
            return matches

        result = benchmark(grep_files)
        assert len(result) >= 100


# =============================================================================
# GREP BENCHMARKS - RUST ACCELERATED (if available)
# =============================================================================


@pytest.mark.benchmark_hash
class TestGrepRustBenchmarks:
    """Benchmarks comparing Rust-accelerated grep to Python."""

    def test_grep_rust_bulk_short_content(self, benchmark, small_scale_data):
        """Benchmark Rust grep_bulk on short content files."""
        from nexus.core.grep_fast import RUST_AVAILABLE, grep_bulk

        grep_dir = small_scale_data / "grep_short"

        # Load file contents
        file_contents = {}
        for filepath in grep_dir.glob("*.log"):
            file_contents[str(filepath)] = filepath.read_bytes()

        def grep_files():
            result = grep_bulk("ERROR", file_contents, ignore_case=False)
            if result is None:
                # Fallback to Python
                import re

                pattern = re.compile(r"ERROR")
                matches = []
                for path, content in file_contents.items():
                    content_str = content.decode("utf-8")
                    for i, line in enumerate(content_str.split("\n")):
                        if pattern.search(line):
                            matches.append({"file": path, "line": i + 1, "content": line})
                return matches
            return result

        result = benchmark(grep_files)
        assert len(result) >= 10

        if RUST_AVAILABLE:
            print("\n[INFO] Rust acceleration was used")
        else:
            print("\n[INFO] Python fallback was used")

    def test_grep_rust_bulk_long_content(self, benchmark, medium_scale_data):
        """Benchmark Rust grep_bulk on long content files."""
        from nexus.core.grep_fast import grep_bulk

        grep_dir = medium_scale_data / "grep_long"

        # Load file contents (limit to first 100 files for memory)
        file_contents = {}
        for i, filepath in enumerate(grep_dir.glob("*.log")):
            if i >= 100:
                break
            file_contents[str(filepath)] = filepath.read_bytes()

        def grep_files():
            result = grep_bulk("ERROR", file_contents, ignore_case=False)
            if result is None:
                import re

                pattern = re.compile(r"ERROR")
                matches = []
                for path, content in file_contents.items():
                    content_str = content.decode("utf-8")
                    for i, line in enumerate(content_str.split("\n")):
                        if pattern.search(line):
                            matches.append({"file": path, "line": i + 1, "content": line})
                return matches
            return result

        result = benchmark(grep_files)
        assert len(result) >= 100

    def test_grep_mmap_long_content(self, benchmark, medium_scale_data):
        """Benchmark Rust mmap grep on long content files."""
        from nexus.core.grep_fast import MMAP_AVAILABLE, grep_files_mmap

        grep_dir = medium_scale_data / "grep_long"
        file_paths = [str(p) for p in grep_dir.glob("*.log")][:100]

        def grep_files():
            result = grep_files_mmap("ERROR", file_paths, ignore_case=False)
            if result is None:
                import re

                pattern = re.compile(r"ERROR")
                matches = []
                for path in file_paths:
                    with open(path) as f:
                        content = f.read()
                    for i, line in enumerate(content.split("\n")):
                        if pattern.search(line):
                            matches.append({"file": path, "line": i + 1, "content": line})
                return matches
            return result

        result = benchmark(grep_files)
        assert len(result) >= 100

        if MMAP_AVAILABLE:
            print("\n[INFO] Mmap acceleration was used")
        else:
            print("\n[INFO] Python fallback was used")


# =============================================================================
# WRITE BENCHMARKS
# =============================================================================


@pytest.mark.benchmark_file_ops
class TestWriteBenchmarks:
    """Benchmarks for writing files at scale."""

    def test_write_100_files_small_content(self, benchmark, tmp_path):
        """Benchmark writing 100 files with small content (~100 bytes)."""
        content = b"small content\n" * 10  # ~140 bytes

        counter = [0]

        def write_files():
            batch_dir = tmp_path / f"batch_{counter[0]}"
            batch_dir.mkdir(exist_ok=True)
            counter[0] += 1
            for i in range(100):
                (batch_dir / f"file_{i:04d}.txt").write_bytes(content)

        benchmark(write_files)

    def test_write_100_files_medium_content(self, benchmark, tmp_path):
        """Benchmark writing 100 files with medium content (~10KB)."""
        content_gen = ContentGenerator(seed=42)
        content = content_gen.generate_log_content(200).encode("utf-8")  # ~10KB

        counter = [0]

        def write_files():
            batch_dir = tmp_path / f"batch_{counter[0]}"
            batch_dir.mkdir(exist_ok=True)
            counter[0] += 1
            for i in range(100):
                (batch_dir / f"file_{i:04d}.txt").write_bytes(content)

        benchmark(write_files)

    def test_write_100_files_large_content(self, benchmark, tmp_path):
        """Benchmark writing 100 files with large content (~100KB)."""
        content_gen = ContentGenerator(seed=42)
        content = content_gen.generate_log_content(2000).encode("utf-8")  # ~100KB

        counter = [0]

        def write_files():
            batch_dir = tmp_path / f"batch_{counter[0]}"
            batch_dir.mkdir(exist_ok=True)
            counter[0] += 1
            for i in range(100):
                (batch_dir / f"file_{i:04d}.txt").write_bytes(content)

        benchmark(write_files)

    def test_write_1000_files_varied_content(self, benchmark, tmp_path):
        """Benchmark writing 1000 files with varied content sizes."""
        content_gen = ContentGenerator(seed=42)

        # Pre-generate content of different sizes
        contents = {
            "tiny": b"tiny\n" * 10,  # ~50 bytes
            "small": content_gen.generate_log_content(20).encode("utf-8"),  # ~1KB
            "medium": content_gen.generate_log_content(200).encode("utf-8"),  # ~10KB
            "large": content_gen.generate_log_content(2000).encode("utf-8"),  # ~100KB
        }

        sizes = list(contents.keys())
        counter = [0]

        def write_files():
            batch_dir = tmp_path / f"batch_{counter[0]}"
            batch_dir.mkdir(exist_ok=True)
            counter[0] += 1
            for i in range(1000):
                size_key = sizes[i % 4]
                (batch_dir / f"file_{i:04d}.txt").write_bytes(contents[size_key])

        benchmark(write_files)

    @pytest.mark.slow
    def test_write_1000_files_sequential(self, benchmark, tmp_path):
        """Benchmark sequential writing of 1000 files."""
        content_gen = ContentGenerator(seed=42)
        content = content_gen.generate_log_content(100).encode("utf-8")

        counter = [0]

        def write_files():
            batch_dir = tmp_path / f"batch_{counter[0]}"
            batch_dir.mkdir(exist_ok=True)
            counter[0] += 1
            for i in range(1000):
                filepath = batch_dir / f"file_{i:04d}.txt"
                filepath.write_bytes(content)
                # Force sync to measure actual write time
                # Note: This is intentionally slow to measure I/O

        benchmark(write_files)


# =============================================================================
# WRITE BENCHMARKS - NEXUS API
# =============================================================================


@pytest.mark.benchmark_file_ops
class TestWriteNexusBenchmarks:
    """Benchmarks for writing files using Nexus API."""

    def test_nexus_write_100_files(self, benchmark, benchmark_nexus):
        """Benchmark writing 100 files via Nexus API."""
        nx = benchmark_nexus
        content_gen = ContentGenerator(seed=42)
        content = content_gen.generate_log_content(20).encode("utf-8")

        counter = [0]

        def write_files():
            counter[0] += 1
            for i in range(100):
                nx.write(f"/batch_{counter[0]}/file_{i:04d}.txt", content)

        benchmark(write_files)

    def test_nexus_write_batch_100_files(self, benchmark, benchmark_nexus):
        """Benchmark batch writing 100 files via Nexus API."""
        nx = benchmark_nexus
        content_gen = ContentGenerator(seed=42)
        content = content_gen.generate_log_content(20).encode("utf-8")

        counter = [0]

        def write_files():
            counter[0] += 1
            batch = [(f"/batch_{counter[0]}/file_{i:04d}.txt", content) for i in range(100)]
            nx.write_batch(batch)

        benchmark(write_files)

    def test_nexus_write_1000_files(self, benchmark, benchmark_nexus):
        """Benchmark writing 1000 files via Nexus API."""
        nx = benchmark_nexus
        content = b"test content\n" * 10

        counter = [0]

        def write_files():
            counter[0] += 1
            for i in range(1000):
                nx.write(f"/batch_{counter[0]}/file_{i:04d}.txt", content)

        benchmark(write_files)

    def test_nexus_write_batch_1000_files(self, benchmark, benchmark_nexus):
        """Benchmark batch writing 1000 files via Nexus API."""
        nx = benchmark_nexus
        content = b"test content\n" * 10

        counter = [0]

        def write_files():
            counter[0] += 1
            batch = [(f"/batch_{counter[0]}/file_{i:04d}.txt", content) for i in range(1000)]
            nx.write_batch(batch)

        benchmark(write_files)


# =============================================================================
# LIST BENCHMARKS - NEXUS API
# =============================================================================


@pytest.mark.benchmark_glob
class TestListNexusBenchmarks:
    """Benchmarks for listing files using Nexus API."""

    @pytest.fixture
    def populated_nexus_large(self, benchmark_nexus):
        """Create a NexusFS with many files for list benchmarks."""
        nx = benchmark_nexus

        # Create 1000 files in flat structure
        for i in range(1000):
            nx.write(f"/flat_files/file_{i:04d}.txt", f"content_{i}".encode())

        # Create nested structure (10 dirs x 10 subdirs x 10 files = 1000 files)
        for i in range(10):
            for j in range(10):
                for k in range(10):
                    nx.write(
                        f"/nested/d{i}/d{j}/file_{k:02d}.txt",
                        f"content_{i}_{j}_{k}".encode(),
                    )

        return nx

    def test_nexus_list_1000_files_flat(self, benchmark, populated_nexus_large):
        """Benchmark listing 1000 files in flat directory via Nexus API."""
        nx = populated_nexus_large

        def list_files():
            return nx.list("/flat_files")

        result = benchmark(list_files)
        assert len(result) == 1000

    def test_nexus_list_recursive(self, benchmark, populated_nexus_large):
        """Benchmark recursive listing via Nexus API."""
        nx = populated_nexus_large

        def list_files():
            return nx.list("/nested", recursive=True)

        result = benchmark(list_files)
        assert len(result) >= 1000

    def test_nexus_glob_pattern(self, benchmark, populated_nexus_large):
        """Benchmark glob pattern matching via Nexus API."""
        nx = populated_nexus_large

        def glob_files():
            return nx.glob("*.txt", "/flat_files")

        result = benchmark(glob_files)
        assert len(result) == 1000

    def test_nexus_glob_recursive_pattern(self, benchmark, populated_nexus_large):
        """Benchmark recursive glob pattern via Nexus API."""
        nx = populated_nexus_large

        def glob_files():
            return nx.glob("**/*.txt", "/nested")

        result = benchmark(glob_files)
        assert len(result) >= 1000


# =============================================================================
# GREP BENCHMARKS - NEXUS API
# =============================================================================


@pytest.mark.benchmark_hash
class TestGrepNexusBenchmarks:
    """Benchmarks for grep operations using Nexus API."""

    @pytest.fixture
    def populated_nexus_grep(self, benchmark_nexus):
        """Create a NexusFS with files for grep benchmarks."""
        nx = benchmark_nexus
        content_gen = ContentGenerator(seed=42)

        # Create files with short content
        for i in range(100):
            content = content_gen.generate_log_content(10)
            nx.write(f"/grep_short/file_{i:04d}.log", content.encode())

        # Create files with long content
        for i in range(100):
            content = content_gen.generate_log_content(100)
            nx.write(f"/grep_long/file_{i:04d}.log", content.encode())

        return nx

    def test_nexus_grep_short_content(self, benchmark, populated_nexus_grep):
        """Benchmark grep on short content files via Nexus API."""
        nx = populated_nexus_grep

        def grep_files():
            return nx.grep("ERROR", "/grep_short")

        result = benchmark(grep_files)
        assert len(result) >= 10

    def test_nexus_grep_long_content(self, benchmark, populated_nexus_grep):
        """Benchmark grep on long content files via Nexus API."""
        nx = populated_nexus_grep

        def grep_files():
            return nx.grep("ERROR", "/grep_long")

        result = benchmark(grep_files)
        assert len(result) >= 100

    def test_nexus_grep_regex_pattern(self, benchmark, populated_nexus_grep):
        """Benchmark grep with regex pattern via Nexus API."""
        nx = populated_nexus_grep

        def grep_files():
            return nx.grep(r"\[ERROR\].*\d{4}", "/grep_long")

        result = benchmark(grep_files)
        assert len(result) >= 50

    def test_nexus_grep_case_insensitive(self, benchmark, populated_nexus_grep):
        """Benchmark case-insensitive grep via Nexus API."""
        nx = populated_nexus_grep

        def grep_files():
            return nx.grep("error", "/grep_long", ignore_case=True)

        result = benchmark(grep_files)
        assert len(result) >= 100
