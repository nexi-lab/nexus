#!/usr/bin/env python3
"""Test write_batch optimization - compare individual vs batch writes."""

import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nexus.remote.client import RemoteNexusFS

NEXUS_URL = os.getenv("NEXUS_URL", "http://localhost:2026")
NEXUS_API_KEY = os.getenv(
    "NEXUS_API_KEY", "sk-default_admin_dddddddd_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
)

NUM_FILES = 50  # Number of files to test


def test_individual_writes(client, test_dir):
    """Test writing files individually."""
    print(f"\n[Individual Writes] Writing {NUM_FILES} files one by one...")

    start = time.time()
    for i in range(NUM_FILES):
        path = f"{test_dir}/individual/file_{i:04d}.txt"
        content = f"Content for file {i}\n".encode() * 10  # ~200 bytes each
        client.write(path, content)
        if (i + 1) % 10 == 0:
            elapsed = time.time() - start
            rate = (i + 1) / elapsed
            print(f"  Progress: {i+1}/{NUM_FILES} ({rate:.1f} files/sec)")

    total_time = time.time() - start
    per_file = (total_time / NUM_FILES) * 1000  # ms

    print(f"\n  Total: {total_time:.2f}s")
    print(f"  Per file: {per_file:.1f}ms")
    print(f"  Rate: {NUM_FILES/total_time:.1f} files/sec")

    return total_time, per_file


def test_batch_writes(client, test_dir):
    """Test writing files in a single batch."""
    print(f"\n[Batch Write] Writing {NUM_FILES} files in one batch...")

    # Prepare all files
    files = []
    for i in range(NUM_FILES):
        path = f"{test_dir}/batch/file_{i:04d}.txt"
        content = f"Content for file {i}\n".encode() * 10  # ~200 bytes each
        files.append((path, content))

    start = time.time()
    results = client.write_batch(files)
    total_time = time.time() - start
    per_file = (total_time / NUM_FILES) * 1000  # ms

    print(f"\n  Total: {total_time:.2f}s")
    print(f"  Per file: {per_file:.1f}ms")
    print(f"  Rate: {NUM_FILES/total_time:.1f} files/sec")
    print(f"  Files written: {len(results)}")

    return total_time, per_file


def main():
    print("=" * 70)
    print("WRITE_BATCH OPTIMIZATION TEST")
    print("=" * 70)
    print(f"Server: {NEXUS_URL}")
    print(f"Files: {NUM_FILES}")

    client = RemoteNexusFS(server_url=NEXUS_URL, api_key=NEXUS_API_KEY)

    # Create unique test directory
    test_dir = f"/batch_test_{uuid.uuid4().hex[:8]}"
    client.mkdir(test_dir)
    client.mkdir(f"{test_dir}/individual")
    client.mkdir(f"{test_dir}/batch")

    try:
        # Test 1: Individual writes
        individual_total, individual_per_file = test_individual_writes(client, test_dir)

        # Test 2: Batch writes
        batch_total, batch_per_file = test_batch_writes(client, test_dir)

        # Summary
        speedup = individual_total / batch_total if batch_total > 0 else 0

        print("\n" + "=" * 70)
        print("SUMMARY")
        print("=" * 70)
        print(f"\n{'Method':<20} {'Total Time':<15} {'Per File':<15} {'Rate':<15}")
        print("-" * 70)
        print(f"{'Individual':<20} {individual_total:>10.2f}s    {individual_per_file:>10.1f}ms   {NUM_FILES/individual_total:>10.1f}/sec")
        print(f"{'Batch':<20} {batch_total:>10.2f}s    {batch_per_file:>10.1f}ms   {NUM_FILES/batch_total:>10.1f}/sec")
        print("-" * 70)
        print(f"\nSpeedup: {speedup:.1f}x faster with batch!")

    finally:
        # Cleanup
        print("\n[Cleanup] Deleting test directory...")
        try:
            client.delete(test_dir)
        except Exception as e:
            print(f"  Warning: {e}")

    print("\nDone! Check /tmp/nexus-debug.log for [WRITE-BATCH-PERF] logs.")


if __name__ == "__main__":
    main()
