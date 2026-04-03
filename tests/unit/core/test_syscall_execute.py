"""Tests for Kernel sys_read / sys_write (Phase E / E.1 / G).

Verifies the full Rust data path: validate -> route -> dcache -> CAS read/write.

Phase E.1: CAS engines are now owned by MountEntry (via add_mount's local_root
parameter), not registered separately on Kernel.
Phase G: renamed execute_read -> sys_read, execute_write -> sys_write.
"""

import tempfile
from pathlib import Path

import pytest
from nexus_fast import (
    Kernel,
    hash_bytes,
)

# ── Action constants (mirror kernel.rs) ──────────────────────────────

# Must match kernel.rs ACTION_* constants
ACTION_DCACHE_HIT = 0
ACTION_RESOLVED = 1
ACTION_PIPE = 2
ACTION_STREAM = 3
ACTION_EXTERNAL = 4
ACTION_CACHE_MISS = 5
ACTION_ERROR = 6

# Entry type constants
DT_REG = 0
DT_DIR = 1
DT_PIPE = 3
DT_STREAM = 4
DT_EXTERNAL = 5


@pytest.fixture
def kernel():
    """Create Kernel with root mount (no CAS)."""
    k = Kernel()
    k.add_mount("/", "root", False, False, "balanced")
    return k


@pytest.fixture
def cas_dir():
    """Temporary directory for CAS storage."""
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def kernel_with_cas(cas_dir):
    """Create Kernel with local CAS on root mount (Phase E.1)."""
    k = Kernel()
    # Root mount with CAS backend
    k.add_mount("/", "root", False, False, "balanced", "local", cas_dir, False)
    return k, cas_dir


# ── Kernel construction ──────────────────────────────────────────────


class TestKernelConstruction:
    def test_basic_construction(self, kernel):
        assert kernel is not None

    def test_repr(self, kernel):
        r = repr(kernel)
        assert "Kernel" in r


# ── sys_read ─────────────────────────────────────────────────────────


class TestExecuteRead:
    def test_full_rust_path(self, kernel_with_cas):
        """DCached hit + local CAS -> bytes returned entirely from Rust."""
        engine, cas_dir = kernel_with_cas
        content = b"hello from Rust execute_read"
        content_hash = hash_bytes(content)

        # Write CAS blob manually
        cas_path = Path(cas_dir) / "cas" / content_hash[:2] / content_hash[2:4] / content_hash
        cas_path.parent.mkdir(parents=True, exist_ok=True)
        cas_path.write_bytes(content)

        # Put in dcache
        engine.dcache_put(
            "/workspace/test.txt", "local", content_hash, len(content), DT_REG, etag=content_hash
        )

        # Execute read
        result = engine.sys_read("/workspace/test.txt", "root", False)
        assert result.hit is True
        assert result.data == content

    def test_dcache_miss_returns_miss(self, kernel_with_cas):
        """DCache miss -> hit=false (Python fallback)."""
        engine, _ = kernel_with_cas
        result = engine.sys_read("/workspace/missing.txt", "root", False)
        assert result.hit is False

    def test_no_cas_backend_returns_miss(self, kernel):
        """Mount without CAS (no local_root) -> hit=false (Python fallback)."""
        kernel.dcache_put(
            "/workspace/test.txt", "s3-backend", "test.txt", 100, DT_REG, etag="hash123"
        )
        result = kernel.sys_read("/workspace/test.txt", "root", False)
        assert result.hit is False

    def test_no_etag_returns_miss(self, kernel_with_cas):
        """Entry without etag (e.g. new file) -> hit=false."""
        engine, _ = kernel_with_cas
        engine.dcache_put("/workspace/new.txt", "local", "", 0, DT_REG)
        result = engine.sys_read("/workspace/new.txt", "root", False)
        assert result.hit is False

    def test_pipe_entry_returns_miss(self, kernel_with_cas):
        """DT_PIPE entries are handled by Python PipeManager."""
        engine, _ = kernel_with_cas
        engine.dcache_put("/pipes/fifo", "local", "", 0, DT_PIPE)
        result = engine.sys_read("/pipes/fifo", "root", False)
        assert result.hit is False

    def test_cas_read_failure_returns_miss(self, kernel_with_cas):
        """CAS blob not on disk -> hit=false (Python fallback)."""
        engine, _ = kernel_with_cas
        engine.dcache_put(
            "/workspace/test.txt",
            "local",
            "test.txt",
            100,
            DT_REG,
            etag="nonexistenthash0000000000000000000000000000000000000000000000",
        )
        result = engine.sys_read("/workspace/test.txt", "root", False)
        assert result.hit is False

    def test_large_file(self, kernel_with_cas):
        """Verify large file read works."""
        engine, cas_dir = kernel_with_cas
        content = b"x" * (512 * 1024)  # 512KB
        content_hash = hash_bytes(content)

        cas_path = Path(cas_dir) / "cas" / content_hash[:2] / content_hash[2:4] / content_hash
        cas_path.parent.mkdir(parents=True, exist_ok=True)
        cas_path.write_bytes(content)

        engine.dcache_put(
            "/workspace/big.bin", "local", content_hash, len(content), DT_REG, etag=content_hash
        )
        result = engine.sys_read("/workspace/big.bin", "root", False)
        assert result.hit is True
        assert len(result.data) == 512 * 1024


# ── sys_write ────────────────────────────────────────────────────────


class TestExecuteWrite:
    def test_full_rust_write(self, kernel_with_cas):
        """DCache hit + local CAS -> content hash returned."""
        engine, cas_dir = kernel_with_cas
        engine.dcache_put("/workspace/test.txt", "local", "", 0, DT_REG, etag="oldhash")

        content = b"new content from Rust execute_write"
        result = engine.sys_write("/workspace/test.txt", "root", content, False)
        assert result.hit is True
        assert len(result.content_id) == 64  # BLAKE3 hex

        # Verify content was actually written to CAS
        cid = result.content_id
        cas_path = Path(cas_dir) / "cas" / cid[:2] / cid[2:4] / cid
        assert cas_path.exists()
        assert cas_path.read_bytes() == content

    def test_dcache_miss_returns_miss(self, kernel_with_cas):
        """DCache miss -> hit=false (Python handles new file)."""
        engine, _ = kernel_with_cas
        result = engine.sys_write("/workspace/new.txt", "root", b"data", False)
        assert result.hit is False

    def test_no_cas_backend_returns_miss(self, kernel):
        """Mount without CAS -> hit=false."""
        kernel.dcache_put("/workspace/test.txt", "s3-remote", "", 0, DT_REG, etag="hash")
        result = kernel.sys_write("/workspace/test.txt", "root", b"data", False)
        assert result.hit is False

    def test_readonly_returns_miss(self, cas_dir):
        """Write to readonly mount -> hit=false (Python handles error)."""
        kernel = Kernel()
        kernel.add_mount("/", "root", False, False, "balanced")
        kernel.add_mount("/readonly", "root", True, False, "balanced", "local", cas_dir, False)
        kernel.dcache_put("/readonly/file.txt", "local", "", 0, DT_REG, etag="hash")
        # PR B returns cache miss on route failure -> returns hit=false
        result = kernel.sys_write("/readonly/file.txt", "root", b"data", False)
        assert result.hit is False

    def test_write_dedup(self, kernel_with_cas):
        """Writing same content twice returns same hash (CAS dedup)."""
        engine, _ = kernel_with_cas
        engine.dcache_put("/workspace/a.txt", "local", "", 0, DT_REG, etag="old1")
        engine.dcache_put("/workspace/b.txt", "local", "", 0, DT_REG, etag="old2")

        content = b"deduplicated content"
        r1 = engine.sys_write("/workspace/a.txt", "root", content, False)
        r2 = engine.sys_write("/workspace/b.txt", "root", content, False)
        assert r1.content_id == r2.content_id


# ── Arc sharing ───────────────────────────────────────────────────────


class TestArcSharing:
    def test_dcache_updates_visible(self, kernel_with_cas):
        """DCache entries added after engine creation are visible."""
        engine, cas_dir = kernel_with_cas
        content = b"added after init"
        content_hash = hash_bytes(content)

        # Write CAS blob
        cas_path = Path(cas_dir) / "cas" / content_hash[:2] / content_hash[2:4] / content_hash
        cas_path.parent.mkdir(parents=True, exist_ok=True)
        cas_path.write_bytes(content)

        # Initially miss
        assert engine.sys_read("/workspace/late.txt", "root", False).hit is False

        # Add to dcache (simulating metastore.put dual-write)
        engine.dcache_put(
            "/workspace/late.txt", "local", content_hash, len(content), DT_REG, etag=content_hash
        )

        # Now should hit
        result = engine.sys_read("/workspace/late.txt", "root", False)
        assert result.hit is True
        assert result.data == content

    def test_mount_updates_visible(self, cas_dir):
        """Mounts added after engine creation are visible."""
        kernel = Kernel()
        kernel.add_mount("/", "root", False, False, "balanced")

        content = b"mount test"
        content_hash = hash_bytes(content)
        cas_path = Path(cas_dir) / "cas" / content_hash[:2] / content_hash[2:4] / content_hash
        cas_path.parent.mkdir(parents=True, exist_ok=True)
        cas_path.write_bytes(content)

        # Add mount for /workspace with CAS
        kernel.add_mount("/workspace", "root", False, False, "fast", "local", cas_dir, False)
        kernel.dcache_put(
            "/workspace/file.txt", "local", content_hash, len(content), DT_REG, etag=content_hash
        )

        result = kernel.sys_read("/workspace/file.txt", "root", False)
        assert result.hit is True
        assert result.data == content
