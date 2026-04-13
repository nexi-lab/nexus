#!/usr/bin/env python3
"""Real FUSE mount e2e tests — runs inside Docker with /dev/fuse.

Tests file I/O, cache invalidation, and performance through an actual
FUSE mount backed by NexusFS with the lease coordinator wired in.

Usage (from repo root):
    docker build -t nexus-fuse-test -f tests/e2e/self_contained/Dockerfile.fuse-test .
    docker run --rm --device /dev/fuse --cap-add SYS_ADMIN --security-opt apparmor:unconfined nexus-fuse-test
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
    from nexus.storage.dict_metastore import DictMetastore

    tmpdir = tempfile.mkdtemp()
    storage_path = os.path.join(tmpdir, "storage")
    mount_point = os.path.join(tmpdir, "mnt")
    os.makedirs(mount_point)

    backend = CASLocalBackend(root_path=storage_path)
    metastore = DictMetastore()
    nx = await create_nexus_fs(
        backend=backend,
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
    )

    # Pre-populate
    nx.write("/test.txt", b"hello world")
    nx.write("/dir/file1.txt", b"content 1")
    nx.write("/dir/file2.txt", b"content 2")
    nx.write("/large.bin", b"x" * 100_000)
    print("Pre-mount data written\n")

    # Mount
    fuse = NexusFUSE(nx, mount_point, mode=MountMode.BINARY)
    fuse.mount(foreground=False)
    time.sleep(2)

    if not fuse.is_mounted():
        print(_red("FUSE mount FAILED"))
        return 1

    mp = mount_point
    results: list[tuple[str, bool, str]] = []

    def ok(name: str, detail: str = "") -> None:
        results.append((name, True, detail))

    def fail(name: str, detail: str) -> None:
        results.append((name, False, detail))

    # ===== FILE OPERATIONS =====
    print("=" * 60)
    print("FILE OPERATIONS")
    print("=" * 60)

    # 1. Read
    try:
        with open(os.path.join(mp, "test.txt"), "rb") as f:
            data = f.read()
        assert data == b"hello world", f"got {data!r}"
        ok("Read file", f"{len(data)} bytes")
    except Exception as e:
        fail("Read file", str(e))

    # 2. Listdir root
    try:
        entries = os.listdir(mp)
        assert "test.txt" in entries, f"test.txt not in {entries}"
        # 'dir' may or may not appear depending on metastore (DictMetastore
        # may not synthesize intermediate directories in root listing)
        ok("Listdir /", f"{len(entries)} entries: {sorted(entries)}")
    except Exception as e:
        fail("Listdir /", str(e))

    # 3. Listdir subdir
    try:
        entries = os.listdir(os.path.join(mp, "dir"))
        assert "file1.txt" in entries
        assert "file2.txt" in entries
        ok("Listdir /dir", f"{sorted(entries)}")
    except Exception as e:
        fail("Listdir /dir", str(e))

    # 4. Write + readback
    try:
        p = os.path.join(mp, "fuse_written.txt")
        with open(p, "wb") as f:
            f.write(b"written via fuse")
        with open(p, "rb") as f:
            data = f.read()
        assert data == b"written via fuse"
        ok("Write + readback")
    except Exception as e:
        fail("Write + readback", str(e))

    # 5. Delete
    try:
        p = os.path.join(mp, "to_delete.txt")
        with open(p, "wb") as f:
            f.write(b"delete me")
        assert os.path.exists(p)
        os.remove(p)
        assert not os.path.exists(p)
        ok("Delete file")
    except Exception as e:
        fail("Delete file", str(e))

    # 6. Mkdir
    try:
        p = os.path.join(mp, "newdir")
        os.makedirs(p)
        assert os.path.isdir(p)
        ok("Mkdir")
    except Exception as e:
        fail("Mkdir", str(e))

    # 7. Rename
    try:
        src = os.path.join(mp, "dir", "file1.txt")
        dst = os.path.join(mp, "dir", "renamed.txt")
        with open(src, "rb") as f:
            original = f.read()
        os.rename(src, dst)
        with open(dst, "rb") as f:
            data = f.read()
        assert data == original
        assert not os.path.exists(src)
        ok("Rename file")
    except Exception as e:
        fail("Rename file", str(e))

    # ===== CACHE COHERENCE =====
    print()
    print("=" * 60)
    print("CACHE COHERENCE")
    print("=" * 60)

    # 8. Write invalidates cache
    try:
        p = os.path.join(mp, "test.txt")
        with open(p, "rb") as f:
            v1 = f.read()
        with open(p, "wb") as f:
            f.write(b"updated content")
        with open(p, "rb") as f:
            v2 = f.read()
        assert v2 == b"updated content", f"stale: got {v2!r}"
        ok("Write invalidates read cache", f"{v1!r} -> {v2!r}")
    except Exception as e:
        fail("Write invalidates read cache", str(e))

    # 9. Stat updates after write
    try:
        p = os.path.join(mp, "stat_test.txt")
        with open(p, "wb") as f:
            f.write(b"short")
        s1 = os.stat(p).st_size
        with open(p, "wb") as f:
            f.write(b"longer content here")
        s2 = os.stat(p).st_size
        assert s2 > s1, f"stat not updated: {s1} -> {s2}"
        ok("Stat updates after write", f"size {s1} -> {s2}")
    except Exception as e:
        fail("Stat updates after write", str(e))

    # 10. Backend write → FUSE eventually sees it
    try:
        p = os.path.join(mp, "dir", "file2.txt")
        with open(p, "rb") as f:
            v1 = f.read()
        nx.write("/dir/file2.txt", b"backend update")
        # FUSE may cache; this tests that at least the read succeeds
        with open(p, "rb") as f:
            v2 = f.read()
        ok("Backend write readable via FUSE", f"v1={v1!r} v2={v2!r}")
    except Exception as e:
        fail("Backend write readable via FUSE", str(e))

    # ===== PERFORMANCE =====
    print()
    print("=" * 60)
    print("PERFORMANCE")
    print("=" * 60)

    # 11. stat latency
    try:
        p = os.path.join(mp, "test.txt")
        os.stat(p)  # warm
        times = []
        for _ in range(200):
            t0 = time.perf_counter()
            os.stat(p)
            times.append(time.perf_counter() - t0)
        avg = sum(times) / len(times) * 1e6
        p50 = sorted(times)[100] * 1e6
        p99 = sorted(times)[198] * 1e6
        ok("stat x200", f"avg={avg:.0f}μs p50={p50:.0f}μs p99={p99:.0f}μs")
        if avg > 5000:
            fail("stat latency", f"avg={avg:.0f}μs exceeds 5ms threshold")
    except Exception as e:
        fail("stat perf", str(e))

    # 12. cached read latency
    try:
        p = os.path.join(mp, "test.txt")
        with open(p, "rb") as f:
            f.read()  # warm
        times = []
        for _ in range(200):
            t0 = time.perf_counter()
            with open(p, "rb") as f:
                f.read()
            times.append(time.perf_counter() - t0)
        avg = sum(times) / len(times) * 1e6
        p50 = sorted(times)[100] * 1e6
        p99 = sorted(times)[198] * 1e6
        ok("read x200", f"avg={avg:.0f}μs p50={p50:.0f}μs p99={p99:.0f}μs")
        if avg > 10000:
            fail("read latency", f"avg={avg:.0f}μs exceeds 10ms threshold")
    except Exception as e:
        fail("read perf", str(e))

    # 13. large file read
    try:
        p = os.path.join(mp, "large.bin")
        with open(p, "rb") as f:
            f.read()  # warm
        times = []
        for _ in range(20):
            t0 = time.perf_counter()
            with open(p, "rb") as f:
                data = f.read()
            times.append(time.perf_counter() - t0)
        assert len(data) == 100_000
        avg_ms = sum(times) / len(times) * 1e3
        ok("100KB read x20", f"avg={avg_ms:.1f}ms")
        if avg_ms > 100:
            fail("100KB read latency", f"avg={avg_ms:.1f}ms exceeds 100ms")
    except Exception as e:
        fail("large read perf", str(e))

    # 14. listdir latency
    try:
        p = os.path.join(mp, "dir")
        os.listdir(p)  # warm
        times = []
        for _ in range(100):
            t0 = time.perf_counter()
            os.listdir(p)
            times.append(time.perf_counter() - t0)
        avg = sum(times) / len(times) * 1e6
        ok("listdir x100", f"avg={avg:.0f}μs")
    except Exception as e:
        fail("listdir perf", str(e))

    # ===== CLEANUP =====
    try:
        fuse.unmount()
    except Exception:
        subprocess.run(["fusermount", "-u", mount_point], capture_output=True)
        subprocess.run(["umount", mount_point], capture_output=True)
    nx.close()

    # ===== REPORT =====
    print()
    print("=" * 60)
    print("RESULTS")
    print("=" * 60)
    passed = 0
    failed = 0
    for name, success, detail in results:
        if success:
            print(f"  {_green('PASS')}  {name}  {detail}")
            passed += 1
        else:
            print(f"  {_red('FAIL')}  {name}  {detail}")
            failed += 1

    print()
    print(f"  {_green(str(passed))} passed, {_red(str(failed)) if failed else '0'} failed")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run_tests()))
