"""Benchmark: CAS volume packing metadata overhead.

Measures per-blob metadata overhead for volume-packed storage vs
one-file-per-hash, proving the issue #3403 claim:
    - Volume: < 40 bytes per entry (index entry = 24B key + 24B value = 48B,
      but amortized with shared volume header)
    - File: 256-536 bytes per file (inode metadata)

Usage:
    python tests/benchmarks/bench_cas_volume_overhead.py [--count 10000]
"""

from __future__ import annotations

import argparse
import tempfile
import time
from pathlib import Path


def measure_file_per_blob(root: Path, count: int) -> dict:
    """Measure overhead of one-file-per-hash CAS layout."""
    cas_dir = root / "cas"
    cas_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    for i in range(count):
        h = f"{i:064x}"
        d1 = cas_dir / h[:2]
        d2 = d1 / h[2:4]
        d2.mkdir(parents=True, exist_ok=True)
        (d2 / h).write_bytes(b"x" * 100)  # 100-byte blob
    write_time = time.perf_counter() - t0

    # Measure disk usage
    total_disk = sum(f.stat().st_blocks * 512 for f in cas_dir.rglob("*") if f.is_file())
    total_content = count * 100

    # Count inodes (files + directories)
    file_count = sum(1 for _ in cas_dir.rglob("*") if _.is_file())
    dir_count = sum(1 for _ in cas_dir.rglob("*") if _.is_dir())

    return {
        "type": "file-per-blob",
        "count": count,
        "total_disk_bytes": total_disk,
        "total_content_bytes": total_content,
        "overhead_bytes": total_disk - total_content,
        "overhead_per_blob": (total_disk - total_content) / count,
        "files": file_count,
        "dirs": dir_count,
        "inodes": file_count + dir_count,
        "write_time_s": write_time,
    }


def measure_volume_packed(root: Path, count: int) -> dict | None:
    """Measure overhead of volume-packed CAS storage."""
    try:
        from nexus_runtime import BlobPackEngine
    except ImportError:
        return None

    vol_dir = root / "volumes"

    t0 = time.perf_counter()
    engine = BlobPackEngine(str(vol_dir), target_volume_size=64 * 1024 * 1024)
    for i in range(count):
        h = f"{i:064x}"
        engine.put(h, b"x" * 100)
    engine.seal_active()
    engine.close()
    write_time = time.perf_counter() - t0

    # Measure disk usage (volume files + index)
    total_disk = sum(f.stat().st_blocks * 512 for f in vol_dir.rglob("*") if f.is_file())
    total_content = count * 100

    file_count = sum(1 for _ in vol_dir.rglob("*") if _.is_file())
    dir_count = sum(1 for _ in vol_dir.rglob("*") if _.is_dir())

    return {
        "type": "volume-packed",
        "count": count,
        "total_disk_bytes": total_disk,
        "total_content_bytes": total_content,
        "overhead_bytes": total_disk - total_content,
        "overhead_per_blob": (total_disk - total_content) / count,
        "files": file_count,
        "dirs": dir_count,
        "inodes": file_count + dir_count,
        "write_time_s": write_time,
    }


def main():
    parser = argparse.ArgumentParser(description="CAS volume overhead benchmark")
    parser.add_argument("--count", type=int, default=10_000, help="Number of blobs")
    parser.add_argument(
        "--full-compaction",
        action="store_true",
        help="Run the real 1GB compaction benchmark (slow, ~30s+)",
    )
    args = parser.parse_args()

    count = args.count
    print(f"\n{'=' * 60}")
    print(f"CAS Volume Packing Overhead Benchmark ({count:,} blobs)")
    print(f"{'=' * 60}\n")

    # File-per-blob
    with tempfile.TemporaryDirectory() as tmpdir:
        file_result = measure_file_per_blob(Path(tmpdir), count)

    # Volume-packed
    vol_result = None
    with tempfile.TemporaryDirectory() as tmpdir:
        vol_result = measure_volume_packed(Path(tmpdir), count)

    # Report
    print(f"{'Metric':<30} {'File-per-blob':>15} {'Volume-packed':>15}")
    print(f"{'-' * 60}")
    print(
        f"{'Total disk (bytes)':<30} {file_result['total_disk_bytes']:>15,} {vol_result['total_disk_bytes'] if vol_result else 'N/A':>15}"
    )
    print(
        f"{'Content (bytes)':<30} {file_result['total_content_bytes']:>15,} {vol_result['total_content_bytes'] if vol_result else 'N/A':>15}"
    )
    print(
        f"{'Overhead (bytes)':<30} {file_result['overhead_bytes']:>15,} {vol_result['overhead_bytes'] if vol_result else 'N/A':>15}"
    )
    print(
        f"{'Overhead per blob (bytes)':<30} {file_result['overhead_per_blob']:>15.1f} {vol_result['overhead_per_blob'] if vol_result else 'N/A':>15}"
    )
    print(
        f"{'Files':<30} {file_result['files']:>15,} {vol_result['files'] if vol_result else 'N/A':>15}"
    )
    print(
        f"{'Directories':<30} {file_result['dirs']:>15,} {vol_result['dirs'] if vol_result else 'N/A':>15}"
    )
    print(
        f"{'Total inodes':<30} {file_result['inodes']:>15,} {vol_result['inodes'] if vol_result else 'N/A':>15}"
    )
    print(
        f"{'Write time (s)':<30} {file_result['write_time_s']:>15.3f} {vol_result['write_time_s'] if vol_result else 'N/A':>15}"
    )

    if vol_result:
        ratio = file_result["overhead_per_blob"] / max(vol_result["overhead_per_blob"], 1)
        inode_ratio = file_result["inodes"] / max(vol_result["inodes"], 1)
        print(f"\n{'=' * 60}")
        print(f"Overhead reduction: {ratio:.1f}x")
        print(f"Inode reduction: {inode_ratio:.0f}x")
        print(f"Volume overhead per blob: {vol_result['overhead_per_blob']:.1f} bytes", end="")
        if vol_result["overhead_per_blob"] < 40:
            print(" ✓ (< 40 bytes target)")
        else:
            print(" ✗ (> 40 bytes target)")

    # Run without volume engine — still useful to show file-per-blob overhead
    if not vol_result:
        print("\n[!] nexus_runtime.BlobPackEngine not available — volume benchmark skipped")
        print(f"    File-per-blob overhead: {file_result['overhead_per_blob']:.1f} bytes/blob")
        print("    This demonstrates the problem volumes solve.")

    # Compaction benchmark (Issue #3408)
    if vol_result:
        print(f"\n{'=' * 60}")
        print("Compaction Benchmark (Issue #3408)")
        print(f"{'=' * 60}\n")
        bench_compaction(count)

    # Full 1GB compaction benchmark (Issue #3408 acceptance criterion)
    if vol_result and args.full_compaction:
        print(f"\n{'=' * 60}")
        print("Full 1GB Compaction Benchmark (Acceptance Criterion)")
        print(f"{'=' * 60}\n")
        # 1GB with 1KB blobs = 1M entries. Use 4KB blobs = 256K entries.
        blob_size = 4096
        full_count = (1024 * 1024 * 1024) // blob_size  # ~262144 entries
        bench_compaction(full_count, blob_size=blob_size)


def bench_compaction(count: int = 10_000, blob_size: int = 100) -> dict | None:
    """Benchmark compaction throughput.

    Acceptance criterion from Issue #3408:
        'compaction of 1GB volume with 50% dead < 10s'

    Args:
        count: Number of blobs to write.
        blob_size: Size of each blob in bytes. Use --full-compaction
            to run the real 1GB benchmark with 4KB blobs.
    """
    try:
        from nexus_runtime import BlobPackEngine
    except ImportError:
        print("[!] nexus_runtime.BlobPackEngine not available — compaction benchmark skipped")
        return None

    with tempfile.TemporaryDirectory() as tmpdir:
        vol_dir = Path(tmpdir) / "compact_bench"
        engine = BlobPackEngine(
            str(vol_dir),
            target_volume_size=64 * 1024 * 1024,
            compaction_bytes_per_cycle=0,  # Unlimited
            compaction_sparsity_threshold=0.3,
        )

        # Write all entries
        t0 = time.perf_counter()
        for i in range(count):
            h = f"{i:064x}"
            engine.put(h, b"x" * blob_size)
        engine.seal_active()
        write_time = time.perf_counter() - t0

        # Delete 50%
        delete_count = count // 2
        t0 = time.perf_counter()
        for i in range(delete_count):
            h = f"{i:064x}"
            engine.delete(h)
        delete_time = time.perf_counter() - t0

        # Measure disk before compaction
        disk_before = sum(f.stat().st_blocks * 512 for f in vol_dir.rglob("*") if f.is_file())

        # Run compaction
        t0 = time.perf_counter()
        compacted, moved, reclaimed = engine.compact()
        compact_time = time.perf_counter() - t0

        # Measure disk after compaction
        disk_after = sum(f.stat().st_blocks * 512 for f in vol_dir.rglob("*") if f.is_file())

        engine.close()

    live_count = count - delete_count
    throughput_mb = (live_count * blob_size / 1024 / 1024) / max(compact_time, 0.001)

    result = {
        "count": count,
        "blob_size": blob_size,
        "delete_pct": 50,
        "write_time_s": write_time,
        "delete_time_s": delete_time,
        "compact_time_s": compact_time,
        "volumes_compacted": compacted,
        "blobs_moved": moved,
        "bytes_reclaimed": reclaimed,
        "disk_before": disk_before,
        "disk_after": disk_after,
        "throughput_mb_s": throughput_mb,
    }

    print(f"{'Entries':<30} {count:>15,}")
    print(f"{'Blob size (bytes)':<30} {blob_size:>15}")
    print(f"{'Deleted':<30} {delete_count:>15,} (50%)")
    print(f"{'Write time (s)':<30} {write_time:>15.3f}")
    print(f"{'Delete time (s)':<30} {delete_time:>15.3f}")
    print(f"{'Compaction time (s)':<30} {compact_time:>15.3f}")
    print(f"{'Volumes compacted':<30} {compacted:>15}")
    print(f"{'Blobs moved':<30} {moved:>15,}")
    print(f"{'Bytes reclaimed':<30} {reclaimed:>15,}")
    print(f"{'Disk before (bytes)':<30} {disk_before:>15,}")
    print(f"{'Disk after (bytes)':<30} {disk_after:>15,}")
    print(f"{'Throughput (MB/s)':<30} {throughput_mb:>15.1f}")

    # Validate acceptance criterion: 1GB with 50% dead < 10s
    total_data = count * blob_size
    is_full_benchmark = total_data >= 1_000_000_000  # ~1GB
    if is_full_benchmark:
        # Real 1GB benchmark — report actual time
        print(f"\n{'Actual 1GB time (s)':<30} {compact_time:>15.3f}", end="")
        if compact_time < 10.0:
            print(" PASS (< 10s)")
        else:
            print(" FAIL (> 10s)")
    else:
        # Smaller benchmark — project linearly
        projected_1gb_time = compact_time * (1_073_741_824 / max(total_data, 1))
        print(f"\n{'Projected 1GB time (s)':<30} {projected_1gb_time:>15.1f}", end="")
        if projected_1gb_time < 10.0:
            print(" PASS (< 10s, projected)")
        else:
            print(" FAIL (> 10s, projected — run with --full-compaction for real benchmark)")

    return result


if __name__ == "__main__":
    main()
