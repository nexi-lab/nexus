"""Performance test data generator for Nexus filesystem benchmarks.

This module generates test data for benchmarking:
1. Listing 50k files (flat and nested, long and short filenames)
2. Grep operations (flat and nested, long and short content)
3. Write operations (1k files)

Usage:
    python -m benchmarks.performance_test_data.generate_data --output-dir /tmp/nexus_perf_data

    # Or in Python:
    from benchmarks.performance_test_data.generate_data import PerformanceDataGenerator
    generator = PerformanceDataGenerator("/tmp/nexus_perf_data")
    generator.generate_all()
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import string
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator


@dataclass
class GenerationConfig:
    """Configuration for test data generation."""

    # File listing benchmarks
    flat_file_count: int = 50_000
    nested_file_count: int = 50_000
    nested_depth: int = 10
    nested_width: int = 10  # Files per directory at each level

    # Filename variations
    short_name_len: int = 8
    long_name_len: int = 128

    # Content variations for grep
    short_content_lines: int = 10
    long_content_lines: int = 1000
    medium_content_lines: int = 100

    # Write benchmarks
    write_file_count: int = 1000

    # Random seed for reproducibility
    seed: int = 42


class ContentGenerator:
    """Generates various types of file content for grep benchmarks."""

    PATTERNS = {
        "error": "[ERROR] {timestamp} - {message}",
        "warning": "[WARN] {timestamp} - {message}",
        "info": "[INFO] {timestamp} - {message}",
        "debug": "[DEBUG] {timestamp} - {message}",
    }

    ERROR_MESSAGES = [
        "Connection refused to database server",
        "Failed to authenticate user",
        "Timeout waiting for response",
        "Memory allocation failed",
        "Disk quota exceeded",
        "Permission denied accessing resource",
        "Invalid configuration detected",
        "Service unavailable",
    ]

    CODE_TEMPLATES = [
        "def {name}(self, {args}) -> {ret}:",
        "class {name}({base}):",
        "    return {value}",
        "    raise {exception}({message})",
        "# TODO: {comment}",
        "async def {name}({args}):",
        "    await {call}()",
        "import {module}",
    ]

    def __init__(self, seed: int = 42):
        self.rng = random.Random(seed)

    def generate_log_content(self, num_lines: int, error_density: float = 0.1) -> str:
        """Generate realistic log file content.

        Args:
            num_lines: Number of log lines to generate
            error_density: Fraction of lines that should be errors (0.0 to 1.0)

        Returns:
            Log file content as string
        """
        lines = []
        for i in range(num_lines):
            timestamp = f"2024-01-15 10:{i // 60:02d}:{i % 60:02d}.{self.rng.randint(0, 999):03d}"

            if self.rng.random() < error_density:
                pattern = self.PATTERNS["error"]
                message = self.rng.choice(self.ERROR_MESSAGES)
            elif self.rng.random() < 0.2:
                pattern = self.PATTERNS["warning"]
                message = f"Slow operation detected: {self.rng.randint(100, 5000)}ms"
            elif self.rng.random() < 0.1:
                pattern = self.PATTERNS["debug"]
                message = f"Processing item {i} of {num_lines}"
            else:
                pattern = self.PATTERNS["info"]
                message = f"Request {self.rng.randint(1000, 9999)} processed successfully"

            lines.append(pattern.format(timestamp=timestamp, message=message))

        return "\n".join(lines)

    def generate_code_content(self, num_lines: int, complexity: str = "medium") -> str:
        """Generate Python-like code content.

        Args:
            num_lines: Number of lines to generate
            complexity: 'simple', 'medium', or 'complex'

        Returns:
            Code content as string
        """
        lines = []
        indent_level = 0
        class_count = 0
        func_count = 0

        for i in range(num_lines):
            indent = "    " * indent_level

            if i % 50 == 0 and complexity != "simple":
                # New class
                class_count += 1
                lines.append(f"class MyClass{class_count}(BaseClass):")
                indent_level = 1
            elif i % 10 == 0:
                # New function
                func_count += 1
                args = ", ".join(
                    [f"arg{j}: str" for j in range(self.rng.randint(1, 4))]
                )
                lines.append(f"{indent}def method_{func_count}(self, {args}) -> int:")
                indent_level = min(indent_level + 1, 4)
            elif i % 5 == 0:
                # Comment or TODO
                lines.append(
                    f"{indent}# TODO: implement feature #{self.rng.randint(100, 999)}"
                )
            elif i % 3 == 0 and complexity == "complex":
                # Exception handling
                lines.append(f"{indent}raise ValueError('Error at line {i}')")
            else:
                # Return statement
                lines.append(f"{indent}return {self.rng.randint(0, 1000)}")

            # Occasionally reduce indent
            if self.rng.random() < 0.1 and indent_level > 0:
                indent_level -= 1

        return "\n".join(lines)

    def generate_json_content(self, num_items: int) -> str:
        """Generate JSON-like content for grep tests."""
        items = []
        for i in range(num_items):
            item = {
                "id": i,
                "name": f"item_{i}",
                "value": self.rng.randint(0, 10000),
                "active": self.rng.choice([True, False]),
                "tags": [f"tag_{j}" for j in range(self.rng.randint(1, 5))],
            }
            items.append(str(item))
        return "\n".join(items)

    def generate_mixed_content(self, num_lines: int) -> str:
        """Generate mixed content (logs, code, json) for realistic grep tests."""
        sections = []
        remaining = num_lines

        while remaining > 0:
            section_type = self.rng.choice(["log", "code", "json"])
            section_size = min(self.rng.randint(10, 100), remaining)

            if section_type == "log":
                sections.append(self.generate_log_content(section_size))
            elif section_type == "code":
                sections.append(self.generate_code_content(section_size))
            else:
                sections.append(self.generate_json_content(section_size))

            remaining -= section_size

        return "\n\n".join(sections)


class FilenameGenerator:
    """Generates various filename patterns for listing benchmarks."""

    EXTENSIONS = [".txt", ".py", ".json", ".log", ".md", ".yaml", ".xml", ".csv"]
    PREFIXES = ["file_", "test_", "data_", "log_", "config_", "output_", "temp_"]

    def __init__(self, seed: int = 42):
        self.rng = random.Random(seed)

    def short_name(self, index: int, length: int = 8) -> str:
        """Generate a short filename."""
        base = f"{index:04d}"
        ext = self.rng.choice(self.EXTENSIONS)
        return f"f{base}{ext}"

    def long_name(self, index: int, length: int = 128) -> str:
        """Generate a long filename."""
        prefix = self.rng.choice(self.PREFIXES)
        suffix = "".join(
            self.rng.choices(string.ascii_lowercase + string.digits, k=length - 20)
        )
        ext = self.rng.choice(self.EXTENSIONS)
        return f"{prefix}{index:06d}_{suffix}{ext}"

    def mixed_name(self, index: int) -> str:
        """Generate a name that's sometimes short, sometimes long."""
        if self.rng.random() < 0.5:
            return self.short_name(index)
        return self.long_name(index)


class PerformanceDataGenerator:
    """Main generator for performance test data."""

    def __init__(self, output_dir: str | Path, config: GenerationConfig | None = None):
        self.output_dir = Path(output_dir)
        self.config = config or GenerationConfig()
        self.content_gen = ContentGenerator(self.config.seed)
        self.filename_gen = FilenameGenerator(self.config.seed)
        self.rng = random.Random(self.config.seed)

    def _ensure_dir(self, path: Path) -> None:
        """Ensure directory exists."""
        path.mkdir(parents=True, exist_ok=True)

    def _progress(
        self, iterable: Iterator, total: int, desc: str, interval: int = 1000
    ) -> Iterator:
        """Simple progress indicator."""
        start_time = time.time()
        for i, item in enumerate(iterable):
            if i % interval == 0 or i == total - 1:
                elapsed = time.time() - start_time
                rate = i / elapsed if elapsed > 0 else 0
                eta = (total - i) / rate if rate > 0 else 0
                print(
                    f"\r{desc}: {i + 1}/{total} ({100 * (i + 1) / total:.1f}%) "
                    f"[{elapsed:.1f}s elapsed, ~{eta:.1f}s remaining]",
                    end="",
                    flush=True,
                )
            yield item
        print()  # New line after completion

    def generate_flat_files_short_names(self, count: int | None = None) -> Path:
        """Generate flat directory with short filenames for list benchmarks.

        Structure:
            flat_short/
                f0000.txt
                f0001.py
                ...
        """
        count = count or self.config.flat_file_count
        out_dir = self.output_dir / "flat_short"
        self._ensure_dir(out_dir)

        print(f"Generating {count} flat files with short names...")

        for i in self._progress(range(count), count, "Flat short"):
            filename = self.filename_gen.short_name(i)
            filepath = out_dir / filename
            # Create minimal content for pure listing benchmarks
            filepath.write_text(f"content_{i}")

        return out_dir

    def generate_flat_files_long_names(self, count: int | None = None) -> Path:
        """Generate flat directory with long filenames for list benchmarks.

        Structure:
            flat_long/
                file_000000_aBcDeFgHiJkLmNoPqRsTuVwXyZ...txt
                ...
        """
        count = count or self.config.flat_file_count
        out_dir = self.output_dir / "flat_long"
        self._ensure_dir(out_dir)

        print(f"Generating {count} flat files with long names...")

        for i in self._progress(range(count), count, "Flat long"):
            filename = self.filename_gen.long_name(i, self.config.long_name_len)
            filepath = out_dir / filename
            filepath.write_text(f"content_{i}")

        return out_dir

    def generate_nested_files_short_names(self, count: int | None = None) -> Path:
        """Generate nested directory structure with short filenames.

        Structure:
            nested_short/
                d0/
                    d0/
                        f0000.txt
                        ...
                    d1/
                        ...
                d1/
                    ...
        """
        count = count or self.config.nested_file_count
        out_dir = self.output_dir / "nested_short"
        self._ensure_dir(out_dir)

        print(f"Generating {count} nested files with short names...")

        depth = self.config.nested_depth
        width = self.config.nested_width

        # Calculate files per leaf directory
        total_leaves = width**depth
        files_per_leaf = max(1, count // total_leaves)

        file_count = 0
        total_to_create = min(count, total_leaves * files_per_leaf)

        def create_nested(current_path: Path, current_depth: int) -> None:
            nonlocal file_count
            if file_count >= count:
                return

            if current_depth >= depth:
                # Create files at leaf
                for _ in range(files_per_leaf):
                    if file_count >= count:
                        break
                    filename = self.filename_gen.short_name(file_count)
                    (current_path / filename).write_text(f"content_{file_count}")
                    file_count += 1
                    if file_count % 1000 == 0:
                        print(
                            f"\rNested short: {file_count}/{total_to_create} "
                            f"({100 * file_count / total_to_create:.1f}%)",
                            end="",
                            flush=True,
                        )
            else:
                for i in range(width):
                    if file_count >= count:
                        break
                    subdir = current_path / f"d{i}"
                    self._ensure_dir(subdir)
                    create_nested(subdir, current_depth + 1)

        create_nested(out_dir, 0)
        print()

        return out_dir

    def generate_nested_files_long_names(self, count: int | None = None) -> Path:
        """Generate nested directory structure with long filenames.

        Structure:
            nested_long/
                directory_level_0_name_abcdefghij.../
                    directory_level_1_name_klmnopqrs.../
                        file_000000_aBcDeFgHiJkLmNoPqRsT...txt
                        ...
        """
        count = count or self.config.nested_file_count
        out_dir = self.output_dir / "nested_long"
        self._ensure_dir(out_dir)

        print(f"Generating {count} nested files with long names...")

        depth = self.config.nested_depth
        width = self.config.nested_width

        # Use shorter depth to keep paths manageable
        effective_depth = min(depth, 5)  # Limit depth for long names
        total_leaves = width**effective_depth
        files_per_leaf = max(1, count // total_leaves)

        file_count = 0
        total_to_create = min(count, total_leaves * files_per_leaf)

        def make_long_dirname(level: int, index: int) -> str:
            suffix = "".join(self.rng.choices(string.ascii_lowercase, k=30))
            return f"dir_l{level}_i{index}_{suffix}"

        def create_nested(current_path: Path, current_depth: int) -> None:
            nonlocal file_count
            if file_count >= count:
                return

            if current_depth >= effective_depth:
                for _ in range(files_per_leaf):
                    if file_count >= count:
                        break
                    filename = self.filename_gen.long_name(
                        file_count, self.config.long_name_len // 2
                    )  # Shorter for deep paths
                    (current_path / filename).write_text(f"content_{file_count}")
                    file_count += 1
                    if file_count % 1000 == 0:
                        print(
                            f"\rNested long: {file_count}/{total_to_create} "
                            f"({100 * file_count / total_to_create:.1f}%)",
                            end="",
                            flush=True,
                        )
            else:
                for i in range(width):
                    if file_count >= count:
                        break
                    dirname = make_long_dirname(current_depth, i)
                    subdir = current_path / dirname
                    self._ensure_dir(subdir)
                    create_nested(subdir, current_depth + 1)

        create_nested(out_dir, 0)
        print()

        return out_dir

    def generate_grep_files_short_content(self, count: int = 1000) -> Path:
        """Generate files with short content for grep benchmarks.

        Each file has ~10 lines of content.
        """
        out_dir = self.output_dir / "grep_short"
        self._ensure_dir(out_dir)

        print(f"Generating {count} files with short content for grep...")

        for i in self._progress(range(count), count, "Grep short"):
            filename = f"file_{i:06d}.log"
            content = self.content_gen.generate_log_content(
                self.config.short_content_lines
            )
            (out_dir / filename).write_text(content)

        return out_dir

    def generate_grep_files_long_content(self, count: int = 1000) -> Path:
        """Generate files with long content for grep benchmarks.

        Each file has ~1000 lines of content.
        """
        out_dir = self.output_dir / "grep_long"
        self._ensure_dir(out_dir)

        print(f"Generating {count} files with long content for grep...")

        for i in self._progress(range(count), count, "Grep long", interval=100):
            filename = f"file_{i:06d}.log"
            content = self.content_gen.generate_log_content(
                self.config.long_content_lines
            )
            (out_dir / filename).write_text(content)

        return out_dir

    def generate_grep_nested_structure(self, total_files: int = 5000) -> Path:
        """Generate nested structure with varied content for grep benchmarks.

        Structure:
            grep_nested/
                logs/
                    app/
                        *.log (short content)
                    system/
                        *.log (long content)
                code/
                    src/
                        *.py (code content)
                    tests/
                        *.py (code content)
                data/
                    *.json (JSON content)
        """
        out_dir = self.output_dir / "grep_nested"
        self._ensure_dir(out_dir)

        print(f"Generating {total_files} nested files with varied content for grep...")

        files_per_category = total_files // 5
        file_count = 0

        # Logs - app (short)
        logs_app = out_dir / "logs" / "app"
        self._ensure_dir(logs_app)
        for i in range(files_per_category):
            content = self.content_gen.generate_log_content(
                self.config.short_content_lines
            )
            (logs_app / f"app_{i:04d}.log").write_text(content)
            file_count += 1
            if file_count % 500 == 0:
                print(
                    f"\rGrep nested: {file_count}/{total_files}",
                    end="",
                    flush=True,
                )

        # Logs - system (long)
        logs_sys = out_dir / "logs" / "system"
        self._ensure_dir(logs_sys)
        for i in range(files_per_category):
            content = self.content_gen.generate_log_content(
                self.config.long_content_lines
            )
            (logs_sys / f"system_{i:04d}.log").write_text(content)
            file_count += 1
            if file_count % 500 == 0:
                print(
                    f"\rGrep nested: {file_count}/{total_files}",
                    end="",
                    flush=True,
                )

        # Code - src
        code_src = out_dir / "code" / "src"
        self._ensure_dir(code_src)
        for i in range(files_per_category):
            content = self.content_gen.generate_code_content(
                self.config.medium_content_lines, "complex"
            )
            (code_src / f"module_{i:04d}.py").write_text(content)
            file_count += 1
            if file_count % 500 == 0:
                print(
                    f"\rGrep nested: {file_count}/{total_files}",
                    end="",
                    flush=True,
                )

        # Code - tests
        code_tests = out_dir / "code" / "tests"
        self._ensure_dir(code_tests)
        for i in range(files_per_category):
            content = self.content_gen.generate_code_content(
                self.config.medium_content_lines, "simple"
            )
            (code_tests / f"test_{i:04d}.py").write_text(content)
            file_count += 1
            if file_count % 500 == 0:
                print(
                    f"\rGrep nested: {file_count}/{total_files}",
                    end="",
                    flush=True,
                )

        # Data
        data_dir = out_dir / "data"
        self._ensure_dir(data_dir)
        for i in range(files_per_category):
            content = self.content_gen.generate_json_content(
                self.config.medium_content_lines
            )
            (data_dir / f"data_{i:04d}.json").write_text(content)
            file_count += 1
            if file_count % 500 == 0:
                print(
                    f"\rGrep nested: {file_count}/{total_files}",
                    end="",
                    flush=True,
                )

        print()
        return out_dir

    def generate_write_benchmark_data(self) -> dict[str, bytes]:
        """Generate content for write benchmarks.

        Returns a dictionary of content to write, with various sizes.
        """
        print(f"Generating {self.config.write_file_count} write contents...")

        contents = {}
        for i in range(self.config.write_file_count):
            size_category = i % 4
            if size_category == 0:
                # Tiny: ~100 bytes
                content = f"tiny_content_{i}\n" * 5
            elif size_category == 1:
                # Small: ~1KB
                content = self.content_gen.generate_log_content(20)
            elif size_category == 2:
                # Medium: ~10KB
                content = self.content_gen.generate_log_content(200)
            else:
                # Large: ~100KB
                content = self.content_gen.generate_log_content(2000)

            contents[f"write_{i:06d}.txt"] = content.encode("utf-8")

        return contents

    def generate_all(self, scale: float = 1.0) -> dict[str, Path]:
        """Generate all test data with optional scaling.

        Args:
            scale: Scaling factor (0.1 = 10% of default sizes, 1.0 = full size)

        Returns:
            Dictionary of data category to output path
        """
        if scale != 1.0:
            self.config.flat_file_count = int(self.config.flat_file_count * scale)
            self.config.nested_file_count = int(self.config.nested_file_count * scale)
            self.config.write_file_count = int(self.config.write_file_count * scale)

        print(f"\n{'=' * 60}")
        print(f"Generating performance test data")
        print(f"Output directory: {self.output_dir}")
        print(f"Scale factor: {scale}")
        print(f"{'=' * 60}\n")

        results = {}

        # List benchmarks
        results["flat_short"] = self.generate_flat_files_short_names()
        results["flat_long"] = self.generate_flat_files_long_names()
        results["nested_short"] = self.generate_nested_files_short_names()
        results["nested_long"] = self.generate_nested_files_long_names()

        # Grep benchmarks
        results["grep_short"] = self.generate_grep_files_short_content()
        results["grep_long"] = self.generate_grep_files_long_content()
        results["grep_nested"] = self.generate_grep_nested_structure()

        print(f"\n{'=' * 60}")
        print("Generation complete!")
        print(f"{'=' * 60}\n")

        return results

    def get_summary(self) -> dict:
        """Get summary of generated data."""
        summary = {
            "output_dir": str(self.output_dir),
            "config": {
                "flat_file_count": self.config.flat_file_count,
                "nested_file_count": self.config.nested_file_count,
                "nested_depth": self.config.nested_depth,
                "nested_width": self.config.nested_width,
                "write_file_count": self.config.write_file_count,
            },
            "categories": {},
        }

        for subdir in self.output_dir.iterdir():
            if subdir.is_dir():
                file_count = sum(1 for _ in subdir.rglob("*") if _.is_file())
                total_size = sum(f.stat().st_size for f in subdir.rglob("*") if f.is_file())
                summary["categories"][subdir.name] = {
                    "file_count": file_count,
                    "total_size_mb": round(total_size / (1024 * 1024), 2),
                }

        return summary


def main():
    parser = argparse.ArgumentParser(
        description="Generate performance test data for Nexus benchmarks"
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="/tmp/nexus_perf_data",
        help="Output directory for generated data",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="Scale factor (0.01 = 1%%, 0.1 = 10%%, 1.0 = full size)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducibility",
    )
    parser.add_argument(
        "--category",
        type=str,
        choices=[
            "all",
            "flat_short",
            "flat_long",
            "nested_short",
            "nested_long",
            "grep_short",
            "grep_long",
            "grep_nested",
        ],
        default="all",
        help="Category of data to generate",
    )

    args = parser.parse_args()

    config = GenerationConfig(seed=args.seed)
    generator = PerformanceDataGenerator(args.output_dir, config)

    if args.category == "all":
        generator.generate_all(scale=args.scale)
    else:
        # Generate specific category
        method_name = f"generate_{args.category.replace('_', '_files_') if 'flat' in args.category or 'nested' in args.category else args.category.replace('_', '_files_')}"
        if args.category.startswith("flat"):
            method_name = f"generate_flat_files_{'short' if 'short' in args.category else 'long'}_names"
        elif args.category.startswith("nested"):
            method_name = f"generate_nested_files_{'short' if 'short' in args.category else 'long'}_names"
        elif args.category == "grep_short":
            method_name = "generate_grep_files_short_content"
        elif args.category == "grep_long":
            method_name = "generate_grep_files_long_content"
        elif args.category == "grep_nested":
            method_name = "generate_grep_nested_structure"

        if hasattr(generator, method_name):
            getattr(generator, method_name)()
        else:
            print(f"Unknown method: {method_name}")
            sys.exit(1)

    # Print summary
    import json

    summary = generator.get_summary()
    print("\nData Summary:")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
