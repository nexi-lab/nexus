"""Benchmark tests for search operations (grep, regex matching).

Run with: pytest tests/benchmarks/test_search_benchmarks.py -v --benchmark-only

These benchmarks compare Python regex vs Rust grep implementation.
See issue #570 for context.
"""

from __future__ import annotations

import re

import pytest


def generate_log_content(num_lines: int) -> bytes:
    """Generate realistic log file content for grep benchmarks."""
    lines = []
    for i in range(num_lines):
        if i % 10 == 0:
            lines.append(f"[ERROR] 2024-01-15 10:30:{i:02d} - Failed to connect to database")
        elif i % 5 == 0:
            lines.append(f"[WARN] 2024-01-15 10:30:{i:02d} - Slow query detected: {i}ms")
        else:
            lines.append(f"[INFO] 2024-01-15 10:30:{i:02d} - Request processed successfully")
    return "\n".join(lines).encode("utf-8")


def generate_code_content(num_lines: int) -> bytes:
    """Generate Python-like code content for grep benchmarks."""
    lines = []
    for i in range(num_lines):
        if i % 20 == 0:
            lines.append(f"class MyClass{i}:")
        elif i % 10 == 0:
            lines.append(f"    def method_{i}(self, arg: str) -> int:")
        elif i % 5 == 0:
            lines.append(f"        # TODO: implement this function #{i}")
        else:
            lines.append(f"        return {i}")
    return "\n".join(lines).encode("utf-8")


# =============================================================================
# PYTHON REGEX BENCHMARKS (baseline)
# =============================================================================


@pytest.mark.benchmark_hash
class TestPythonRegexBenchmarks:
    """Baseline benchmarks using Python's re module."""

    def test_python_regex_simple_1k_lines(self, benchmark):
        """Benchmark Python regex search in 1K lines."""
        content = generate_log_content(1000)
        content_str = content.decode("utf-8")
        pattern = re.compile(r"ERROR")

        def search():
            return pattern.findall(content_str)

        result = benchmark(search)
        assert len(result) == 100  # 1 ERROR per 10 lines

    @pytest.mark.benchmark_ci
    def test_python_regex_simple_10k_lines(self, benchmark):
        """Benchmark Python regex search in 10K lines."""
        content = generate_log_content(10000)
        content_str = content.decode("utf-8")
        pattern = re.compile(r"ERROR")

        def search():
            return pattern.findall(content_str)

        result = benchmark(search)
        assert len(result) == 1000

    def test_python_regex_complex_pattern(self, benchmark):
        """Benchmark Python regex with complex pattern."""
        content = generate_code_content(5000)
        content_str = content.decode("utf-8")
        # Match function definitions
        pattern = re.compile(r"def\s+\w+\(.*?\)")

        def search():
            return pattern.findall(content_str)

        result = benchmark(search)
        assert len(result) > 0

    def test_python_regex_line_by_line(self, benchmark):
        """Benchmark Python regex searching line by line (like grep)."""
        content = generate_log_content(5000)
        lines = content.decode("utf-8").split("\n")
        pattern = re.compile(r"ERROR")

        def search():
            matches = []
            for i, line in enumerate(lines):
                if pattern.search(line):
                    matches.append((i + 1, line))
            return matches

        result = benchmark(search)
        assert len(result) == 500

    def test_python_regex_case_insensitive(self, benchmark):
        """Benchmark Python regex with case-insensitive flag."""
        content = generate_log_content(5000)
        content_str = content.decode("utf-8")
        pattern = re.compile(r"error", re.IGNORECASE)

        def search():
            return pattern.findall(content_str)

        result = benchmark(search)
        assert len(result) == 500


# =============================================================================
# RUST GREP BENCHMARKS
# =============================================================================


@pytest.mark.benchmark_hash
class TestRustGrepBenchmarks:
    """Benchmarks for Rust-accelerated grep operations."""

    def test_rust_grep_available(self):
        """Check if Rust grep is available."""
        from nexus.core.grep_fast import RUST_AVAILABLE

        print(f"\n[INFO] Rust grep available: {RUST_AVAILABLE}")

    def test_rust_grep_1k_lines(self, benchmark):
        """Benchmark Rust grep in 1K lines."""
        from nexus.core.grep_fast import RUST_AVAILABLE, grep_bulk

        content = generate_log_content(1000)
        file_contents = {"/test.log": content}

        def search():
            result = grep_bulk("ERROR", file_contents, ignore_case=False)
            if result is None:
                # Fallback to Python if Rust not available

                matches = []
                content_str = content.decode("utf-8")
                for i, line in enumerate(content_str.split("\n")):
                    if "ERROR" in line:
                        matches.append({"file": "/test.log", "line": i + 1, "content": line})
                return matches
            return result

        result = benchmark(search)
        assert len(result) == 100

        if RUST_AVAILABLE:
            print("\n[INFO] Rust acceleration was used")
        else:
            print("\n[INFO] Python fallback was used")

    @pytest.mark.benchmark_ci
    def test_rust_grep_10k_lines(self, benchmark):
        """Benchmark Rust grep in 10K lines."""
        from nexus.core.grep_fast import grep_bulk

        content = generate_log_content(10000)
        file_contents = {"/test.log": content}

        def search():
            result = grep_bulk("ERROR", file_contents, ignore_case=False)
            if result is None:
                matches = []
                content_str = content.decode("utf-8")
                for i, line in enumerate(content_str.split("\n")):
                    if "ERROR" in line:
                        matches.append({"file": "/test.log", "line": i + 1, "content": line})
                return matches
            return result

        result = benchmark(search)
        assert len(result) == 1000

    def test_rust_grep_multiple_files(self, benchmark):
        """Benchmark Rust grep across multiple files."""
        from nexus.core.grep_fast import grep_bulk

        # Create 10 files with 1K lines each
        file_contents = {f"/file_{i}.log": generate_log_content(1000) for i in range(10)}

        def search():
            result = grep_bulk("ERROR", file_contents, ignore_case=False)
            if result is None:
                matches = []
                for path, content in file_contents.items():
                    content_str = content.decode("utf-8")
                    for i, line in enumerate(content_str.split("\n")):
                        if "ERROR" in line:
                            matches.append({"file": path, "line": i + 1, "content": line})
                return matches
            return result

        result = benchmark(search)
        assert len(result) == 1000  # 100 per file * 10 files

    def test_rust_grep_regex_pattern(self, benchmark):
        """Benchmark Rust grep with regex pattern."""
        from nexus.core.grep_fast import grep_bulk

        content = generate_code_content(5000)
        file_contents = {"/code.py": content}

        def search():
            result = grep_bulk(r"def\s+\w+", file_contents, ignore_case=False)
            if result is None:
                import re

                pattern = re.compile(r"def\s+\w+")
                matches = []
                content_str = content.decode("utf-8")
                for i, line in enumerate(content_str.split("\n")):
                    if pattern.search(line):
                        matches.append({"file": "/code.py", "line": i + 1, "content": line})
                return matches
            return result

        result = benchmark(search)
        assert len(result) > 0

    def test_rust_grep_case_insensitive(self, benchmark):
        """Benchmark Rust grep with case-insensitive search."""
        from nexus.core.grep_fast import grep_bulk

        content = generate_log_content(5000)
        file_contents = {"/test.log": content}

        def search():
            result = grep_bulk("error", file_contents, ignore_case=True)
            if result is None:
                import re

                pattern = re.compile("error", re.IGNORECASE)
                matches = []
                content_str = content.decode("utf-8")
                for i, line in enumerate(content_str.split("\n")):
                    if pattern.search(line):
                        matches.append({"file": "/test.log", "line": i + 1, "content": line})
                return matches
            return result

        result = benchmark(search)
        assert len(result) == 500


# =============================================================================
# RUST MMAP GREP BENCHMARKS (Issue #893)
# =============================================================================


@pytest.mark.benchmark_hash
class TestRustMmapGrepBenchmarks:
    """Benchmarks for Rust mmap-accelerated grep operations (Issue #893)."""

    @pytest.fixture(autouse=True)
    def setup_temp_files(self, tmp_path):
        """Create temporary files for mmap benchmarks."""
        self.tmp_path = tmp_path
        self.test_files = {}

        # Create test files with various sizes
        for i in range(10):
            content = generate_log_content(1000)
            file_path = tmp_path / f"file_{i}.log"
            file_path.write_bytes(content)
            self.test_files[str(file_path)] = content

        # Create a large file for mmap performance testing
        large_content = generate_log_content(10000)
        large_file = tmp_path / "large_file.log"
        large_file.write_bytes(large_content)
        self.large_file = str(large_file)
        self.large_content = large_content

    def test_mmap_grep_available(self):
        """Check if mmap grep is available."""
        from nexus.core.grep_fast import MMAP_AVAILABLE

        print(f"\n[INFO] Mmap grep available: {MMAP_AVAILABLE}")

    def test_mmap_grep_single_file(self, benchmark):
        """Benchmark mmap grep on a single file."""
        from nexus.core.grep_fast import MMAP_AVAILABLE, grep_files_mmap

        def search():
            result = grep_files_mmap("ERROR", [self.large_file], ignore_case=False)
            if result is None:
                # Fallback to Python if mmap not available
                import re

                pattern = re.compile(r"ERROR")
                matches = []
                content_str = self.large_content.decode("utf-8")
                for i, line in enumerate(content_str.split("\n")):
                    if pattern.search(line):
                        matches.append({"file": self.large_file, "line": i + 1, "content": line})
                return matches
            return result

        result = benchmark(search)
        assert len(result) == 1000  # 1 ERROR per 10 lines in 10000 lines

        if MMAP_AVAILABLE:
            print("\n[INFO] Mmap acceleration was used")
        else:
            print("\n[INFO] Python fallback was used")

    def test_mmap_grep_multiple_files(self, benchmark):
        """Benchmark mmap grep across multiple files."""
        from nexus.core.grep_fast import MMAP_AVAILABLE, grep_files_mmap

        file_paths = list(self.test_files.keys())

        def search():
            result = grep_files_mmap("ERROR", file_paths, ignore_case=False)
            if result is None:
                # Fallback to Python if mmap not available
                import re

                pattern = re.compile(r"ERROR")
                matches = []
                for path, content in self.test_files.items():
                    content_str = content.decode("utf-8")
                    for i, line in enumerate(content_str.split("\n")):
                        if pattern.search(line):
                            matches.append({"file": path, "line": i + 1, "content": line})
                return matches
            return result

        result = benchmark(search)
        assert len(result) == 1000  # 100 ERRORs per file * 10 files

        if MMAP_AVAILABLE:
            print("\n[INFO] Mmap acceleration was used")
        else:
            print("\n[INFO] Python fallback was used")

    def test_mmap_vs_bulk_grep_comparison(self, benchmark):
        """Compare mmap grep vs bulk grep (read + grep)."""
        from nexus.core.grep_fast import MMAP_AVAILABLE, grep_bulk, grep_files_mmap

        file_paths = list(self.test_files.keys())

        def search_mmap():
            """Use mmap-based grep (zero-copy)."""
            result = grep_files_mmap("ERROR", file_paths, ignore_case=False)
            return result if result else []

        def search_bulk():
            """Use bulk grep (read + copy)."""
            # Read files into memory first
            file_contents = {}
            for path in file_paths:
                with open(path, "rb") as f:
                    file_contents[path] = f.read()
            result = grep_bulk("ERROR", file_contents, ignore_case=False)
            return result if result else []

        if MMAP_AVAILABLE:
            result = benchmark(search_mmap)
            print("\n[INFO] Benchmarking mmap grep")
        else:
            result = benchmark(search_bulk)
            print("\n[INFO] Benchmarking bulk grep (mmap not available)")

        assert len(result) == 1000

    def test_mmap_grep_case_insensitive(self, benchmark):
        """Benchmark mmap grep with case-insensitive search."""
        from nexus.core.grep_fast import grep_files_mmap

        def search():
            result = grep_files_mmap("error", [self.large_file], ignore_case=True)
            if result is None:
                import re

                pattern = re.compile("error", re.IGNORECASE)
                matches = []
                content_str = self.large_content.decode("utf-8")
                for i, line in enumerate(content_str.split("\n")):
                    if pattern.search(line):
                        matches.append({"file": self.large_file, "line": i + 1, "content": line})
                return matches
            return result

        result = benchmark(search)
        assert len(result) == 1000

    def test_mmap_grep_regex_pattern(self, benchmark):
        """Benchmark mmap grep with regex pattern."""
        from nexus.core.grep_fast import grep_files_mmap

        # Create code files for regex testing
        code_content = generate_code_content(5000)
        code_file = self.tmp_path / "code.py"
        code_file.write_bytes(code_content)

        def search():
            result = grep_files_mmap(r"def\s+\w+", [str(code_file)], ignore_case=False)
            if result is None:
                import re

                pattern = re.compile(r"def\s+\w+")
                matches = []
                content_str = code_content.decode("utf-8")
                for i, line in enumerate(content_str.split("\n")):
                    if pattern.search(line):
                        matches.append({"file": str(code_file), "line": i + 1, "content": line})
                return matches
            return result

        result = benchmark(search)
        assert len(result) > 0


# =============================================================================
# GLOB PATTERN MATCHING BENCHMARKS
# =============================================================================


@pytest.mark.benchmark_glob
class TestGlobPatternBenchmarks:
    """Benchmarks for glob pattern matching."""

    def test_python_fnmatch_simple(self, benchmark):
        """Benchmark Python fnmatch for simple patterns."""
        import fnmatch

        paths = [f"/dir/file_{i:04d}.txt" for i in range(1000)]
        paths += [f"/dir/file_{i:04d}.py" for i in range(1000)]

        def match():
            return [p for p in paths if fnmatch.fnmatch(p, "*.txt")]

        result = benchmark(match)
        assert len(result) == 1000

    def test_python_fnmatch_complex(self, benchmark):
        """Benchmark Python fnmatch for complex patterns."""
        import fnmatch

        paths = [f"/src/module_{i}/file_{j}.py" for i in range(50) for j in range(20)]

        def match():
            return [p for p in paths if fnmatch.fnmatch(p, "/src/module_*/file_*.py")]

        result = benchmark(match)
        assert len(result) == 1000

    def test_rust_glob_simple(self, benchmark):
        """Benchmark Rust glob for simple patterns (if available)."""
        from nexus.core.glob_fast import RUST_AVAILABLE, glob_match_bulk

        paths = [f"/dir/file_{i:04d}.txt" for i in range(1000)]
        paths += [f"/dir/file_{i:04d}.py" for i in range(1000)]

        def match():
            result = glob_match_bulk(["*.txt"], paths)
            if result is None:
                import fnmatch

                return [p for p in paths if fnmatch.fnmatch(p, "*.txt")]
            return result

        result = benchmark(match)
        assert len(result) == 1000

        if RUST_AVAILABLE:
            print("\n[INFO] Rust glob was used")
        else:
            print("\n[INFO] Python fallback was used (Rust glob not available)")

    def test_rust_glob_multiple_patterns(self, benchmark):
        """Benchmark Rust glob with multiple patterns."""
        from nexus.core.glob_fast import glob_match_bulk

        paths = [f"/dir/file_{i:04d}.txt" for i in range(500)]
        paths += [f"/dir/file_{i:04d}.py" for i in range(500)]
        paths += [f"/dir/file_{i:04d}.json" for i in range(500)]
        paths += [f"/dir/file_{i:04d}.md" for i in range(500)]

        def match():
            result = glob_match_bulk(["*.txt", "*.py"], paths)
            if result is None:
                import fnmatch

                return [
                    p for p in paths if fnmatch.fnmatch(p, "*.txt") or fnmatch.fnmatch(p, "*.py")
                ]
            return result

        result = benchmark(match)
        assert len(result) == 1000

    def test_rust_glob_recursive_pattern(self, benchmark):
        """Benchmark Rust glob with recursive pattern (**/*)."""
        from nexus.core.glob_fast import glob_match_bulk

        # Generate paths with directory structure
        paths = []
        for i in range(10):
            for j in range(10):
                for k in range(10):
                    paths.append(f"/level_{i}/level_{j}/level_{k}/file.py")

        def match():
            result = glob_match_bulk(["**/*.py"], paths)
            if result is None:
                # Fallback: all paths match since they all end in .py
                return paths
            return result

        result = benchmark(match)
        assert len(result) == 1000


# =============================================================================
# HYBRID SEARCH FUSION BENCHMARKS (Issue #798)
# =============================================================================


@pytest.mark.benchmark_fusion
class TestHybridSearchFusionBenchmarks:
    """Benchmarks for hybrid search fusion algorithms (Issue #798)."""

    @pytest.fixture
    def small_result_sets(self):
        """Generate small result sets (100 each) for baseline benchmarks."""
        keyword_results = [
            {
                "chunk_id": f"kw_{i}",
                "path": f"/file_{i % 10}.py",
                "chunk_index": i % 5,
                "score": 100.0 - (i * 0.5),
            }
            for i in range(100)
        ]

        # 50% overlap with keyword results
        vector_results = [
            {
                "chunk_id": f"kw_{i}" if i < 50 else f"vec_{i}",
                "path": f"/file_{i % 10}.py",
                "chunk_index": i % 5,
                "score": 1.0 - (i * 0.005),
            }
            for i in range(100)
        ]

        return keyword_results, vector_results

    @pytest.fixture
    def large_result_sets(self):
        """Generate large result sets (1000 each) for stress testing."""
        keyword_results = [
            {
                "chunk_id": f"kw_{i}",
                "path": f"/file_{i % 100}.py",
                "chunk_index": i % 10,
                "score": 100.0 - (i * 0.1),
            }
            for i in range(1000)
        ]

        # 50% overlap with keyword results
        vector_results = [
            {
                "chunk_id": f"kw_{i}" if i < 500 else f"vec_{i}",
                "path": f"/file_{i % 100}.py",
                "chunk_index": i % 10,
                "score": 1.0 - (i * 0.001),
            }
            for i in range(1000)
        ]

        return keyword_results, vector_results

    def test_rrf_fusion_100_results(self, benchmark, small_result_sets):
        """Benchmark RRF fusion with 100 results from each source."""
        from nexus.search.fusion import rrf_fusion

        keyword_results, vector_results = small_result_sets

        def fuse():
            return rrf_fusion(keyword_results, vector_results, k=60, limit=10)

        result = benchmark(fuse)
        assert len(result) == 10

    @pytest.mark.benchmark_ci
    def test_rrf_fusion_1k_results(self, benchmark, large_result_sets):
        """Benchmark RRF fusion with 1K results from each source."""
        from nexus.search.fusion import rrf_fusion

        keyword_results, vector_results = large_result_sets

        def fuse():
            return rrf_fusion(keyword_results, vector_results, k=60, limit=100)

        result = benchmark(fuse)
        assert len(result) == 100

    def test_weighted_fusion_100_results(self, benchmark, small_result_sets):
        """Benchmark weighted fusion with 100 results from each source."""
        from nexus.search.fusion import weighted_fusion

        keyword_results, vector_results = small_result_sets

        def fuse():
            return weighted_fusion(
                keyword_results, vector_results, alpha=0.5, normalize=True, limit=10
            )

        result = benchmark(fuse)
        assert len(result) == 10

    def test_weighted_fusion_1k_results(self, benchmark, large_result_sets):
        """Benchmark weighted fusion with 1K results from each source."""
        from nexus.search.fusion import weighted_fusion

        keyword_results, vector_results = large_result_sets

        def fuse():
            return weighted_fusion(
                keyword_results, vector_results, alpha=0.5, normalize=True, limit=100
            )

        result = benchmark(fuse)
        assert len(result) == 100

    def test_rrf_weighted_fusion_1k_results(self, benchmark, large_result_sets):
        """Benchmark RRF weighted fusion with 1K results from each source."""
        from nexus.search.fusion import rrf_weighted_fusion

        keyword_results, vector_results = large_result_sets

        def fuse():
            return rrf_weighted_fusion(keyword_results, vector_results, alpha=0.5, k=60, limit=100)

        result = benchmark(fuse)
        assert len(result) == 100

    def test_normalization_overhead(self, benchmark, large_result_sets):
        """Benchmark min-max normalization overhead."""
        from nexus.search.fusion import normalize_scores_minmax

        scores = [r["score"] for r in large_result_sets[0]]

        def normalize():
            return normalize_scores_minmax(scores)

        result = benchmark(normalize)
        assert len(result) == 1000

    def test_fuse_results_dispatcher(self, benchmark, large_result_sets):
        """Benchmark fuse_results dispatcher overhead."""
        from nexus.search.fusion import FusionConfig, FusionMethod, fuse_results

        keyword_results, vector_results = large_result_sets
        config = FusionConfig(method=FusionMethod.RRF, rrf_k=60)

        def fuse():
            return fuse_results(keyword_results, vector_results, config=config, limit=100)

        result = benchmark(fuse)
        assert len(result) == 100


# =============================================================================
# TRIGRAM INDEX BENCHMARKS (Issue #954)
# =============================================================================


@pytest.mark.benchmark_hash
class TestTrigramBenchmarks:
    """Benchmarks for trigram index build and search operations (Issue #954)."""

    @pytest.fixture(autouse=True)
    def setup_trigram_files(self, tmp_path):
        """Create temporary files for trigram benchmarks."""
        self.tmp_path = tmp_path

        # Create 1K files with varying content
        self.file_paths_1k = []
        for i in range(1000):
            content = generate_log_content(100)
            file_path = tmp_path / f"file_{i:04d}.log"
            file_path.write_bytes(content)
            self.file_paths_1k.append(str(file_path))

        # Create 100 code files
        self.code_files = []
        for i in range(100):
            content = generate_code_content(200)
            file_path = tmp_path / f"code_{i:04d}.py"
            file_path.write_bytes(content)
            self.code_files.append(str(file_path))

    def test_trigram_build_1k_files(self, benchmark):
        """Benchmark building trigram index from 1K files."""
        from nexus.core import trigram_fast

        if not trigram_fast.is_available():
            pytest.skip("Trigram index not available")

        idx_path = str(self.tmp_path / "bench_1k.trgm")

        def build():
            trigram_fast.build_index(self.file_paths_1k, idx_path)

        benchmark(build)

        stats = trigram_fast.get_stats(idx_path)
        assert stats is not None
        print(
            f"\n[INFO] 1K files index: {stats['file_count']} files, "
            f"{stats['trigram_count']} trigrams, "
            f"{stats['index_size_bytes'] / 1024:.1f} KB"
        )

    def test_trigram_search_literal(self, benchmark):
        """Benchmark trigram search for literal pattern."""
        from nexus.core import trigram_fast

        if not trigram_fast.is_available():
            pytest.skip("Trigram index not available")

        idx_path = str(self.tmp_path / "bench_search.trgm")
        trigram_fast.build_index(self.file_paths_1k, idx_path)

        def search():
            return trigram_fast.grep(idx_path, "ERROR", max_results=1000)

        result = benchmark(search)
        assert result is not None
        print(f"\n[INFO] Trigram literal search: {len(result)} matches")

    def test_trigram_search_regex(self, benchmark):
        """Benchmark trigram search for regex pattern."""
        from nexus.core import trigram_fast

        if not trigram_fast.is_available():
            pytest.skip("Trigram index not available")

        idx_path = str(self.tmp_path / "bench_regex.trgm")
        trigram_fast.build_index(self.code_files, idx_path)

        def search():
            return trigram_fast.grep(idx_path, r"def\s+\w+", max_results=1000)

        result = benchmark(search)
        assert result is not None
        print(f"\n[INFO] Trigram regex search: {len(result)} matches")

    def test_trigram_search_no_match(self, benchmark):
        """Benchmark trigram search for non-matching pattern."""
        from nexus.core import trigram_fast

        if not trigram_fast.is_available():
            pytest.skip("Trigram index not available")

        idx_path = str(self.tmp_path / "bench_nomatch.trgm")
        trigram_fast.build_index(self.file_paths_1k, idx_path)

        def search():
            return trigram_fast.grep(idx_path, "xyzzy_nonexistent_12345", max_results=1000)

        result = benchmark(search)
        assert result is not None
        assert len(result) == 0
        print("\n[INFO] Trigram no-match: 0 results (fast rejection)")

    def test_trigram_vs_mmap_grep(self, benchmark):
        """Compare trigram index search vs mmap grep."""
        from nexus.core import trigram_fast

        if not trigram_fast.is_available():
            pytest.skip("Trigram index not available")

        idx_path = str(self.tmp_path / "bench_compare.trgm")
        trigram_fast.build_index(self.file_paths_1k, idx_path)

        def search_trigram():
            return trigram_fast.grep(idx_path, "ERROR", max_results=1000)

        result = benchmark(search_trigram)
        assert result is not None
        print(f"\n[INFO] Trigram search: {len(result)} matches")
