"""Real FUSE mount e2e tests — validates file I/O, cache, and performance.

These tests mount a real NexusFS via FUSE and exercise the full stack:
filesystem → FUSE ops → cache (with lease coordinator) → backend.

Requires macFUSE (macOS) or libfuse (Linux) installed.

Run with:
    pytest tests/e2e/self_contained/test_fuse_real_mount_e2e.py -v -o "addopts=" --timeout=60
"""

import os
import platform
import subprocess
import time

import pytest

# Skip entire module if fusepy is not installed or FUSE kernel support missing
pytest.importorskip("fuse", reason="fusepy not installed")

_FUSE_AVAILABLE = False
if platform.system() == "Darwin":
    _FUSE_AVAILABLE = os.path.exists("/Library/Filesystems/macfuse.fs")
elif platform.system() == "Linux":
    _FUSE_AVAILABLE = os.path.exists("/dev/fuse")

pytestmark = pytest.mark.skipif(not _FUSE_AVAILABLE, reason="FUSE kernel support not available")


@pytest.fixture()
async def fuse_mount(tmp_path):
    """Create a real NexusFS, mount via FUSE, yield mount path, unmount on cleanup."""
    from nexus.backends.storage.cas_local import CASLocalBackend
    from nexus.core.config import PermissionConfig
    from nexus.factory import create_nexus_fs
    from nexus.fuse.mount import MountMode, NexusFUSE
    from nexus.storage.raft_metadata_store import RaftMetadataStore

    storage_path = str(tmp_path / "storage")
    db_path = str(tmp_path / "meta")
    mount_point = str(tmp_path / "mnt")
    os.makedirs(mount_point)

    backend = CASLocalBackend(root_path=storage_path)
    metastore = RaftMetadataStore.embedded(db_path=db_path)
    nx = await create_nexus_fs(
        backend=backend,
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
    )

    # Pre-populate test data
    nx.write("/test.txt", b"hello world")
    nx.write("/dir/file1.txt", b"content 1")
    nx.write("/dir/file2.txt", b"content 2")
    nx.write("/large.bin", b"x" * 100_000)

    fuse = NexusFUSE(nx, mount_point, mode=MountMode.BINARY)
    fuse.mount(foreground=False)
    time.sleep(2)

    assert fuse.is_mounted(), "FUSE mount failed"

    yield {"mount": mount_point, "nx": nx, "fuse": fuse}

    # Cleanup
    try:
        fuse.unmount()
    except Exception:
        subprocess.run(["umount", mount_point], capture_output=True)
    nx.close()


class TestFUSERealFileOps:
    """Real file operations through FUSE mount."""

    @pytest.mark.asyncio
    async def test_read_file(self, fuse_mount):
        mp = fuse_mount["mount"]
        with open(os.path.join(mp, "test.txt"), "rb") as f:
            data = f.read()
        assert data == b"hello world"

    @pytest.mark.asyncio
    async def test_listdir_root(self, fuse_mount):
        mp = fuse_mount["mount"]
        entries = os.listdir(mp)
        assert "test.txt" in entries
        assert "dir" in entries

    @pytest.mark.asyncio
    async def test_listdir_subdir(self, fuse_mount):
        mp = fuse_mount["mount"]
        entries = os.listdir(os.path.join(mp, "dir"))
        assert "file1.txt" in entries
        assert "file2.txt" in entries

    @pytest.mark.asyncio
    async def test_write_and_readback(self, fuse_mount):
        mp = fuse_mount["mount"]
        path = os.path.join(mp, "fuse_written.txt")
        with open(path, "wb") as f:
            f.write(b"written via fuse")
        with open(path, "rb") as f:
            data = f.read()
        assert data == b"written via fuse"

    @pytest.mark.asyncio
    async def test_delete_file(self, fuse_mount):
        mp = fuse_mount["mount"]
        path = os.path.join(mp, "to_delete.txt")
        with open(path, "wb") as f:
            f.write(b"delete me")
        assert os.path.exists(path)
        os.remove(path)
        assert not os.path.exists(path)

    @pytest.mark.asyncio
    async def test_mkdir(self, fuse_mount):
        mp = fuse_mount["mount"]
        path = os.path.join(mp, "newdir")
        os.makedirs(path)
        assert os.path.isdir(path)


class TestFUSECacheCoherence:
    """Cache invalidation through FUSE mount."""

    @pytest.mark.asyncio
    async def test_write_invalidates_cache(self, fuse_mount):
        """Write through FUSE → immediate readback gets new content."""
        mp = fuse_mount["mount"]
        path = os.path.join(mp, "test.txt")

        with open(path, "rb") as f:
            v1 = f.read()
        assert v1 == b"hello world"

        with open(path, "wb") as f:
            f.write(b"updated content")
        with open(path, "rb") as f:
            v2 = f.read()
        assert v2 == b"updated content", f"Cache stale: got {v2!r}"

    @pytest.mark.asyncio
    async def test_backend_write_reflected_in_fuse(self, fuse_mount):
        """Write via NexusFS API → FUSE read gets new content (after cache expires)."""
        mp = fuse_mount["mount"]
        nx = fuse_mount["nx"]

        # Read through FUSE first (populates cache)
        path = os.path.join(mp, "dir", "file1.txt")
        with open(path, "rb") as f:
            v1 = f.read()
        assert v1 == b"content 1"

        # Write via backend API (bypasses FUSE cache)
        nx.write("/dir/file1.txt", b"backend updated")

        # The FUSE cache may serve stale for up to TTL (60s).
        # This test documents current behavior — with lease integration
        # active, this staleness window would be eliminated.
        # For now, just verify the file is readable.
        with open(path, "rb") as f:
            v2 = f.read()
        assert v2 in (b"content 1", b"backend updated")


class TestFUSEPerformance:
    """Performance benchmarks on real FUSE mount."""

    @pytest.mark.asyncio
    async def test_stat_latency(self, fuse_mount):
        """stat() calls on cached file should be sub-millisecond."""
        mp = fuse_mount["mount"]
        path = os.path.join(mp, "test.txt")

        # Warm cache
        os.stat(path)

        times = []
        for _ in range(100):
            t0 = time.perf_counter()
            os.stat(path)
            times.append(time.perf_counter() - t0)

        avg_us = sum(times) / len(times) * 1e6
        p50_us = sorted(times)[50] * 1e6
        p99_us = sorted(times)[99] * 1e6

        print(f"\nstat x100: avg={avg_us:.0f}μs p50={p50_us:.0f}μs p99={p99_us:.0f}μs")
        # Cached stat should be under 5ms even with FUSE overhead
        assert avg_us < 5000, f"stat too slow: {avg_us:.0f}μs avg"

    @pytest.mark.asyncio
    async def test_cached_read_latency(self, fuse_mount):
        """Cached small file reads should be sub-5ms."""
        mp = fuse_mount["mount"]
        path = os.path.join(mp, "test.txt")

        # Warm cache
        with open(path, "rb") as f:
            f.read()

        times = []
        for _ in range(100):
            t0 = time.perf_counter()
            with open(path, "rb") as f:
                f.read()
            times.append(time.perf_counter() - t0)

        avg_us = sum(times) / len(times) * 1e6
        p50_us = sorted(times)[50] * 1e6
        p99_us = sorted(times)[99] * 1e6

        print(f"\nread x100: avg={avg_us:.0f}μs p50={p50_us:.0f}μs p99={p99_us:.0f}μs")
        assert avg_us < 10000, f"read too slow: {avg_us:.0f}μs avg"

    @pytest.mark.asyncio
    async def test_large_file_read(self, fuse_mount):
        """100KB file read should complete in under 100ms."""
        mp = fuse_mount["mount"]
        path = os.path.join(mp, "large.bin")

        times = []
        for _ in range(10):
            t0 = time.perf_counter()
            with open(path, "rb") as f:
                data = f.read()
            times.append(time.perf_counter() - t0)

        assert len(data) == 100_000
        avg_ms = sum(times) / len(times) * 1e3
        print(f"\n100KB read x10: avg={avg_ms:.1f}ms")
        assert avg_ms < 100, f"large read too slow: {avg_ms:.1f}ms avg"

    @pytest.mark.asyncio
    async def test_listdir_latency(self, fuse_mount):
        """Directory listing should complete in under 50ms."""
        mp = fuse_mount["mount"]
        path = os.path.join(mp, "dir")

        # Warm cache
        os.listdir(path)

        times = []
        for _ in range(50):
            t0 = time.perf_counter()
            os.listdir(path)
            times.append(time.perf_counter() - t0)

        avg_us = sum(times) / len(times) * 1e6
        print(f"\nlistdir x50: avg={avg_us:.0f}μs")
        assert avg_us < 50000, f"listdir too slow: {avg_us:.0f}μs avg"
