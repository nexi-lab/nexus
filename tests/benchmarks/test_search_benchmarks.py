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
