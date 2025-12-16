import os
import shutil
import tempfile
import time

import psutil

from nexus.core.grep_fast import grep_bulk


def test_grep_memory_usage():
    # Create a temporary directory with large files
    temp_dir = tempfile.mkdtemp()
    try:
        print(f"Generating test files in {temp_dir}...")
        file_contents = {}
        # Generate 100MB of data
        for i in range(10):
            content = b"some text content with key_word " * 100000  # ~2.8MB per file
            path = os.path.join(temp_dir, f"file_{i}.txt")
            with open(path, "wb") as f:
                f.write(content)
            file_contents[path] = content

        process = psutil.Process(os.getpid())
        mem_before = process.memory_info().rss / 1024 / 1024
        print(f"Memory before grep_bulk: {mem_before:.2f} MB")

        start_time = time.time()
        # This simulates the loading of all files into memory which we already did above,
        # but the point is to show that `grep_bulk` REQUIRES this dictionary.
        # The benchmark here verifies that passing this large dict to Rust works but retains Python memory.

        # We will also try to double the memory usage to simulate "loading" if we read from disk inside the benchmark
        results = grep_bulk("key_word", file_contents)

        mem_after = process.memory_info().rss / 1024 / 1024
        print(f"Memory during/after grep_bulk: {mem_after:.2f} MB")
        print(f"Time taken: {time.time() - start_time:.4f}s")
        print(f"Results found: {len(results) if results else 0}")

    finally:
        shutil.rmtree(temp_dir)


if __name__ == "__main__":
    test_grep_memory_usage()
