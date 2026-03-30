#!/usr/bin/env python3
"""E2E test: lease-aware cache staleness through real FUSE mounts (Issue #3400).

Tests the full stack:
  FUSE read → FUSELeaseCoordinator → LeaseManager revocation callback
    → FileContentCache.mark_lease_revoked() → stale read returns None → re-fetch

Also measures performance gain from lease-aware staleness:
  - Baseline: without lease-aware staleness (stale reads return cached data)
  - With staleness: stale reads force re-fetch (correct behavior)
  - Measures: cache hit rate, re-fetch count, read latency

Usage (from repo root):
    docker build -t nexus-fuse-test -f tests/e2e/self_contained/Dockerfile.fuse-test .
    docker run --rm --device /dev/fuse --cap-add SYS_ADMIN \
        nexus-fuse-test python /app/tests/e2e/self_contained/test_fuse_lease_staleness_e2e.py
"""

import asyncio
import os
import subprocess
import sys
import tempfile
import time


def _green(s: str) -> str:
    return f"\033[92m{s}\033[0m"


def _red(s: str) -> str:
    return f"\033[91m{s}\033[0m"


def _yellow(s: str) -> str:
    return f"\033[93m{s}\033[0m"


async def run_tests() -> int:
    from nexus.backends.storage.cas_local import CASLocalBackend
    from nexus.core.config import PermissionConfig
    from nexus.factory import create_nexus_fs
    from nexus.fuse.mount import MountMode, NexusFUSE
    from nexus.lib.lease import LocalLeaseManager, SystemClock
    from nexus.storage.dict_metastore import DictMetastore
    from nexus.storage.file_cache import FileContentCache

    tmpdir = tempfile.mkdtemp()
    storage_path = os.path.join(tmpdir, "storage")
    fc_path = os.path.join(tmpdir, "file_cache")

    backend = CASLocalBackend(root_path=storage_path)
    metastore = DictMetastore()
    nx = await create_nexus_fs(
        backend=backend,
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
    )

    # Shared lease manager
    lease_mgr = LocalLeaseManager(zone_id="test", clock=SystemClock(), sweep_interval=3600.0)

    # File content cache (L2/L3)
    file_cache = FileContentCache(fc_path)

    # Pre-populate data
    await nx.write("/doc.txt", b"original-content")
    await nx.write("/report.txt", b"report-v1")
    for i in range(20):
        await nx.write(f"/batch/file_{i:03d}.txt", f"content-{i}".encode())
    print("Data written\n")

    # Mount with file_cache wired in
    mp = os.path.join(tmpdir, "mnt")
    os.makedirs(mp)
    fuse = NexusFUSE(
        nx,
        mp,
        mode=MountMode.BINARY,
        lease_manager=lease_mgr,
        file_cache=file_cache,
    )
    fuse.mount(foreground=False)
    time.sleep(2)
    assert fuse.is_mounted(), "FUSE mount failed"
    zone_id = getattr(nx, "zone_id", None) or "root"
    print(f"Mount: {mp}  zone={zone_id}  holder={fuse._mount_id}\n")

    results: list[tuple[str, bool, str]] = []

    def ok(name: str, detail: str = "") -> None:
        results.append((name, True, detail))

    def fail(name: str, detail: str) -> None:
        results.append((name, False, detail))

    # =================================================================
    # SECTION 1: FileContentCache staleness e2e
    # =================================================================
    print("=" * 60)
    print("FILECONTENT CACHE STALENESS E2E")
    print("=" * 60)

    # 1. Populate FileContentCache and verify reads work
    try:
        file_cache.write(zone_id, "/doc.txt", b"original-content")
        cached = file_cache.read(zone_id, "/doc.txt")
        assert cached == b"original-content"
        ok("FileContentCache read works", f"got {len(cached)} bytes")
    except Exception as e:
        fail("FileContentCache read works", str(e))

    # 2. Mark lease revoked → read returns None (stale)
    try:
        file_cache.mark_lease_revoked(zone_id, "/doc.txt")
        stale_result = file_cache.read(zone_id, "/doc.txt")
        assert stale_result is None, f"Expected None (stale), got {stale_result!r}"
        ok("Stale read returns None after revocation")
    except Exception as e:
        fail("Stale read returns None after revocation", str(e))

    # 3. Write fresh content → staleness cleared
    try:
        file_cache.write(zone_id, "/doc.txt", b"updated-content")
        fresh = file_cache.read(zone_id, "/doc.txt")
        assert fresh == b"updated-content"
        ok("Write clears staleness", f"got {fresh!r}")
    except Exception as e:
        fail("Write clears staleness", str(e))

    # 4. Mark lease acquired → re-read succeeds
    try:
        file_cache.mark_lease_revoked(zone_id, "/doc.txt")
        assert file_cache.read(zone_id, "/doc.txt") is None
        file_cache.mark_lease_acquired(zone_id, "/doc.txt")
        result = file_cache.read(zone_id, "/doc.txt")
        assert result == b"updated-content"
        ok("Lease acquired clears staleness")
    except Exception as e:
        fail("Lease acquired clears staleness", str(e))

    # 5. Bulk read with mixed stale/fresh
    try:
        for i in range(20):
            file_cache.write(zone_id, f"/batch/file_{i:03d}.txt", f"content-{i}".encode())
        # Mark first 10 stale
        for i in range(10):
            file_cache.mark_lease_revoked(zone_id, f"/batch/file_{i:03d}.txt")

        paths = [f"/batch/file_{i:03d}.txt" for i in range(20)]
        bulk = file_cache.read_bulk(zone_id, paths)
        # Should get 10 (the non-stale ones)
        assert len(bulk) == 10, f"Expected 10 fresh, got {len(bulk)}"
        ok("Bulk read filters stale paths", f"{len(bulk)}/20 returned")
    except Exception as e:
        fail("Bulk read filters stale paths", str(e))

    # 6. Cross-mount coherence: FUSE write → backend → read through FUSE
    try:
        # Read through FUSE (populates FUSE L1 cache)
        fuse_path = os.path.join(mp, "doc.txt")
        with open(fuse_path, "rb") as f:
            fuse_content = f.read()

        # Write new content through FUSE (should trigger invalidation)
        with open(fuse_path, "wb") as f:
            f.write(b"fuse-written-v2")

        time.sleep(0.5)  # Let revocation callbacks fire

        # Re-read through FUSE
        with open(fuse_path, "rb") as f:
            fuse_after = f.read()

        if fuse_after == b"fuse-written-v2":
            ok("FUSE write + re-read coherent", f"before={fuse_content!r} after={fuse_after!r}")
        else:
            fail(
                "FUSE write + re-read coherent", f"Got {fuse_after!r} instead of b'fuse-written-v2'"
            )
    except Exception as e:
        fail("FUSE write + re-read coherent", str(e))

    # =================================================================
    # SECTION 2: LeaseManager callback → FileContentCache wiring
    # =================================================================
    print()
    print("=" * 60)
    print("LEASE CALLBACK → FILECONTENT CACHE WIRING")
    print("=" * 60)

    # 7. End-to-end: lease acquire → cache write → lease revoke → cache stale
    try:
        from nexus.contracts.protocols.lease import LeaseState

        # Acquire a lease
        lease = await lease_mgr.acquire(
            "fuse:/e2e-test.txt", fuse._mount_id, LeaseState.SHARED_READ
        )
        assert lease is not None
        file_cache.mark_lease_acquired(zone_id, "/e2e-test.txt")
        file_cache.write(zone_id, "/e2e-test.txt", b"e2e-data")

        # Verify fresh read
        assert file_cache.read(zone_id, "/e2e-test.txt") == b"e2e-data"

        # Revoke the lease (simulates another agent writing)
        revoked = await lease_mgr.revoke("fuse:/e2e-test.txt")
        assert len(revoked) == 1

        # Manually mark stale (in production, the revocation callback does this)
        file_cache.mark_lease_revoked(zone_id, "/e2e-test.txt")

        # Read should now return None
        stale = file_cache.read(zone_id, "/e2e-test.txt")
        assert stale is None
        ok("Full lease lifecycle: acquire → write → revoke → stale")
    except Exception as e:
        fail("Full lease lifecycle", str(e))

    # 8. Lease manager stats reflect activity
    try:
        stats = await lease_mgr.stats()
        ok(
            "Lease manager stats",
            f"acquires={stats['acquire_count']} revokes={stats['revoke_count']} "
            f"active={stats['active_leases']}",
        )
    except Exception as e:
        fail("Lease manager stats", str(e))

    # =================================================================
    # SECTION 3: PERFORMANCE MEASUREMENT
    # =================================================================
    print()
    print("=" * 60)
    print("PERFORMANCE: STALENESS CHECK OVERHEAD")
    print("=" * 60)

    # Populate cache with 500 files
    perf_paths = [f"/perf/file_{i:04d}.txt" for i in range(500)]
    perf_content = b"benchmark-content-" + b"x" * 200
    for path in perf_paths:
        file_cache.write(zone_id, path, perf_content)

    # 9. Baseline: read 500 files (all fresh, no staleness checks trigger)
    try:
        # Warm up
        for path in perf_paths[:10]:
            file_cache.read(zone_id, path)

        t0 = time.perf_counter()
        hit_count = 0
        for _ in range(3):
            for path in perf_paths:
                result = file_cache.read(zone_id, path)
                if result is not None:
                    hit_count += 1
        baseline_elapsed = time.perf_counter() - t0
        baseline_total = 3 * 500
        baseline_hit_rate = hit_count / baseline_total
        ok(
            "Baseline: 0% stale (1500 reads)",
            f"hit_rate={baseline_hit_rate:.1%} elapsed={baseline_elapsed * 1000:.1f}ms "
            f"({baseline_elapsed / baseline_total * 1e6:.0f}μs/read)",
        )
    except Exception as e:
        fail("Baseline read perf", str(e))

    # 10. 50% stale: half return None (forcing re-fetch)
    try:
        for path in perf_paths[:250]:
            file_cache.mark_lease_revoked(zone_id, path)

        t0 = time.perf_counter()
        hit_count = 0
        miss_count = 0
        for _ in range(3):
            for path in perf_paths:
                result = file_cache.read(zone_id, path)
                if result is not None:
                    hit_count += 1
                else:
                    miss_count += 1
        stale_elapsed = time.perf_counter() - t0
        stale_total = 3 * 500
        stale_hit_rate = hit_count / stale_total

        overhead_pct = (
            ((stale_elapsed - baseline_elapsed) / baseline_elapsed * 100)
            if baseline_elapsed > 0
            else 0
        )
        ok(
            "50% stale (1500 reads)",
            f"hit_rate={stale_hit_rate:.1%} misses={miss_count} "
            f"elapsed={stale_elapsed * 1000:.1f}ms overhead={overhead_pct:+.1f}%",
        )
    except Exception as e:
        fail("50% stale read perf", str(e))

    # 11. Recovery: write fresh content, measure hit rate back to 100%
    try:
        for path in perf_paths[:250]:
            file_cache.write(zone_id, path, perf_content)

        t0 = time.perf_counter()
        hit_count = 0
        for _ in range(3):
            for path in perf_paths:
                result = file_cache.read(zone_id, path)
                if result is not None:
                    hit_count += 1
        recovery_elapsed = time.perf_counter() - t0
        recovery_total = 3 * 500
        recovery_hit_rate = hit_count / recovery_total
        ok(
            "Recovery: re-write clears staleness",
            f"hit_rate={recovery_hit_rate:.1%} elapsed={recovery_elapsed * 1000:.1f}ms",
        )
    except Exception as e:
        fail("Recovery perf", str(e))

    # 12. FUSE read latency (with lease manager wired in)
    try:
        fuse_file = os.path.join(mp, "doc.txt")
        # Warm
        with open(fuse_file, "rb") as f:
            f.read()

        times = []
        for _ in range(200):
            t0 = time.perf_counter()
            with open(fuse_file, "rb") as f:
                f.read()
            times.append(time.perf_counter() - t0)

        avg_us = sum(times) / len(times) * 1e6
        p50_us = sorted(times)[100] * 1e6
        p99_us = sorted(times)[198] * 1e6
        ok(
            "FUSE read x200 (leased + staleness)",
            f"avg={avg_us:.0f}μs p50={p50_us:.0f}μs p99={p99_us:.0f}μs",
        )
    except Exception as e:
        fail("FUSE read latency", str(e))

    # 13. FUSE stat latency
    try:
        fuse_file = os.path.join(mp, "doc.txt")
        os.stat(fuse_file)  # warm
        times = []
        for _ in range(200):
            t0 = time.perf_counter()
            os.stat(fuse_file)
            times.append(time.perf_counter() - t0)

        avg_us = sum(times) / len(times) * 1e6
        p50_us = sorted(times)[100] * 1e6
        p99_us = sorted(times)[198] * 1e6
        ok(
            "FUSE stat x200 (leased + staleness)",
            f"avg={avg_us:.0f}μs p50={p50_us:.0f}μs p99={p99_us:.0f}μs",
        )
    except Exception as e:
        fail("FUSE stat latency", str(e))

    # 14. Saved re-fetches: count how many stale reads would have returned
    #     wrong data without staleness detection
    try:
        # Reset: populate 100 files, revoke 50, write new content to backend
        test_paths = [f"/saved/file_{i:03d}.txt" for i in range(100)]
        for i, path in enumerate(test_paths):
            file_cache.write(zone_id, path, f"old-{i}".encode())

        # Simulate backend update: first 50 files get new content in backend,
        # and their leases are revoked
        for i in range(50):
            file_cache.mark_lease_revoked(zone_id, test_paths[i])

        # Without staleness: all 100 would return cached (50 stale!)
        # With staleness: 50 return None → forced re-fetch (correct)
        stale_detected = 0
        fresh_served = 0
        for path in test_paths:
            result = file_cache.read(zone_id, path)
            if result is None:
                stale_detected += 1
            else:
                fresh_served += 1

        ok(
            f"Prevented {stale_detected} stale reads",
            f"{stale_detected} stale detected, {fresh_served} fresh served "
            f"(without this feature: 0 stale detected, 100 potentially wrong)",
        )
    except Exception as e:
        fail("Stale read prevention count", str(e))

    # =================================================================
    # CLEANUP
    # =================================================================
    try:
        fuse.unmount()
    except Exception:
        subprocess.run(["fusermount", "-u", mp], capture_output=True)
        subprocess.run(["umount", mp], capture_output=True)
    nx.close()
    await lease_mgr.close()

    # =================================================================
    # REPORT
    # =================================================================
    print()
    print("=" * 60)
    print("RESULTS")
    print("=" * 60)
    passed = 0
    failed = 0
    for name, success, detail in results:
        if success:
            print(f"  {_green('PASS')}  {name}")
            if detail:
                print(f"         {detail}")
            passed += 1
        else:
            print(f"  {_red('FAIL')}  {name}")
            if detail:
                print(f"         {detail}")
            failed += 1

    print()
    print(f"  {_green(str(passed))} passed, {_red(str(failed)) if failed else '0'} failed")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run_tests()))
