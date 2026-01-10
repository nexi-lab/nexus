#!/usr/bin/env python3
"""Generate performance test data for Nexus benchmarks.

Focus on:
1. 50k files flat (for list benchmark)
2. 50k files nested (for list benchmark)
3. Files with SHORT content (~10 lines) for grep
4. Files with LONG content (~1000 lines) for grep
5. 1k files for write benchmark
"""

import os
import random
import time
from pathlib import Path

OUTPUT_DIR = Path("/tmp/nexus_perf_data")
random.seed(42)


def generate_log_line(i: int, is_error: bool = False) -> str:
    """Generate a single log line."""
    timestamp = f"2024-01-15 10:{i // 60 % 60:02d}:{i % 60:02d}"
    if is_error:
        return f"[ERROR] {timestamp} - Connection failed to database server"
    return f"[INFO] {timestamp} - Request {random.randint(1000, 9999)} processed"


def generate_content(num_lines: int, error_rate: float = 0.1) -> str:
    """Generate log content with specified number of lines."""
    lines = []
    for i in range(num_lines):
        is_error = random.random() < error_rate
        lines.append(generate_log_line(i, is_error))
    return "\n".join(lines)


def progress(current: int, total: int, desc: str, start_time: float):
    """Print progress."""
    if current % 1000 == 0 or current == total:
        elapsed = time.time() - start_time
        rate = current / elapsed if elapsed > 0 else 0
        eta = (total - current) / rate if rate > 0 else 0
        print(f"\r{desc}: {current}/{total} ({100*current/total:.1f}%) "
              f"[{elapsed:.1f}s, ~{eta:.1f}s left]", end="", flush=True)


def generate_flat_50k():
    """Generate 50k files in flat directory."""
    print("\n=== Generating 50k flat files ===")
    out_dir = OUTPUT_DIR / "flat_50k"
    out_dir.mkdir(parents=True, exist_ok=True)

    start = time.time()
    for i in range(50_000):
        (out_dir / f"file_{i:05d}.txt").write_text(f"content_{i}")
        progress(i + 1, 50_000, "Flat", start)
    print()


def generate_nested_50k():
    """Generate 50k files in nested directory structure."""
    print("\n=== Generating 50k nested files ===")
    out_dir = OUTPUT_DIR / "nested_50k"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 10 dirs x 10 subdirs x 500 files = 50,000
    start = time.time()
    count = 0
    for i in range(10):
        for j in range(10):
            subdir = out_dir / f"d{i}" / f"d{j}"
            subdir.mkdir(parents=True, exist_ok=True)
            for k in range(500):
                (subdir / f"file_{k:03d}.txt").write_text(f"content_{count}")
                count += 1
                progress(count, 50_000, "Nested", start)
    print()


def generate_grep_short():
    """Generate 1000 files with SHORT content (~10 lines each)."""
    print("\n=== Generating grep files (SHORT content, 10 lines) ===")
    out_dir = OUTPUT_DIR / "grep_short_content"
    out_dir.mkdir(parents=True, exist_ok=True)

    start = time.time()
    for i in range(1000):
        content = generate_content(10)  # 10 lines
        (out_dir / f"file_{i:04d}.log").write_text(content)
        progress(i + 1, 1000, "Grep short", start)
    print()


def generate_grep_long():
    """Generate 1000 files with LONG content (~1000 lines each)."""
    print("\n=== Generating grep files (LONG content, 1000 lines) ===")
    out_dir = OUTPUT_DIR / "grep_long_content"
    out_dir.mkdir(parents=True, exist_ok=True)

    start = time.time()
    for i in range(1000):
        content = generate_content(1000)  # 1000 lines
        (out_dir / f"file_{i:04d}.log").write_text(content)
        progress(i + 1, 1000, "Grep long", start)
    print()


def generate_grep_nested():
    """Generate nested structure with both short and long content."""
    print("\n=== Generating nested grep files (mixed content) ===")
    out_dir = OUTPUT_DIR / "grep_nested"
    out_dir.mkdir(parents=True, exist_ok=True)

    start = time.time()
    count = 0
    total = 2000  # 1000 short + 1000 long

    # Short content in one subdir
    short_dir = out_dir / "short"
    short_dir.mkdir(parents=True, exist_ok=True)
    for i in range(1000):
        content = generate_content(10)
        (short_dir / f"file_{i:04d}.log").write_text(content)
        count += 1
        progress(count, total, "Grep nested", start)

    # Long content in another subdir
    long_dir = out_dir / "long"
    long_dir.mkdir(parents=True, exist_ok=True)
    for i in range(1000):
        content = generate_content(1000)
        (long_dir / f"file_{i:04d}.log").write_text(content)
        count += 1
        progress(count, total, "Grep nested", start)
    print()


def generate_write_1k():
    """Generate content for 1k write benchmark."""
    print("\n=== Generating 1k write benchmark files ===")
    out_dir = OUTPUT_DIR / "write_1k"
    out_dir.mkdir(parents=True, exist_ok=True)

    start = time.time()
    for i in range(1000):
        # Varied content sizes
        if i % 4 == 0:
            content = "tiny\n" * 10  # ~50 bytes
        elif i % 4 == 1:
            content = generate_content(20)  # ~1KB
        elif i % 4 == 2:
            content = generate_content(200)  # ~10KB
        else:
            content = generate_content(500)  # ~25KB
        (out_dir / f"file_{i:04d}.txt").write_text(content)
        progress(i + 1, 1000, "Write 1k", start)
    print()


def main():
    print("=" * 60)
    print("Generating Performance Test Data")
    print(f"Output: {OUTPUT_DIR}")
    print("=" * 60)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    generate_flat_50k()
    generate_nested_50k()
    generate_grep_short()
    generate_grep_long()
    generate_grep_nested()
    generate_write_1k()

    print("\n" + "=" * 60)
    print("DONE! Summary:")
    print("=" * 60)

    for subdir in sorted(OUTPUT_DIR.iterdir()):
        if subdir.is_dir():
            file_count = sum(1 for _ in subdir.rglob("*") if _.is_file())
            total_size = sum(f.stat().st_size for f in subdir.rglob("*") if f.is_file())
            print(f"  {subdir.name}: {file_count:,} files, {total_size/1024/1024:.1f} MB")


if __name__ == "__main__":
    main()
