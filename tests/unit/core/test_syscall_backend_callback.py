"""Tests for Kernel backend I/O -- Kernel Boundary Collapse (section 7 PR 1).

After the boundary collapse, Python backends are removed from Rust.
Mounts with local_root get a CasLocalBackend (pure Rust CAS + local transport).
Mounts without local_root -> sys_read/sys_write return hit=false -> Python full path.
"""

import tempfile
from pathlib import Path

import pytest
from nexus_kernel import (
    Kernel,
    hash_bytes,
)

# Entry type constants
DT_REG = 0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine_with_cas():
    """Kernel with a CAS-backed mount (local_root present)."""
    with tempfile.TemporaryDirectory() as cas_dir:
        kernel = Kernel()
        kernel.add_mount("/", "root", False, False, "balanced", "local", cas_dir, False)
        yield kernel, cas_dir


# ---------------------------------------------------------------------------
# CAS backend works (read + write via Rust CasLocalBackend)
# ---------------------------------------------------------------------------


class TestCASBackendWorks:
    def test_cas_read(self, engine_with_cas):
        """CAS backend serves read when content exists on disk."""
        engine, cas_dir = engine_with_cas
        content = b"cas content for read"
        content_hash = hash_bytes(content)

        # Write CAS blob manually
        cas_path = Path(cas_dir) / "cas" / content_hash[:2] / content_hash[2:4] / content_hash
        cas_path.parent.mkdir(parents=True, exist_ok=True)
        cas_path.write_bytes(content)

        engine.dcache_put(
            "/workspace/f.txt", "local", content_hash, len(content), DT_REG, etag=content_hash
        )

        result = engine.sys_read("/workspace/f.txt", "root", False)
        assert result.hit is True
        assert result.data == content

    def test_cas_write(self, engine_with_cas):
        """CAS backend completes write and returns BLAKE3 hash."""
        engine, cas_dir = engine_with_cas
        engine.dcache_put("/workspace/f.txt", "local", "", 0, DT_REG, etag="old")

        result = engine.sys_write("/workspace/f.txt", "root", b"new data", False)
        assert result.hit is True
        assert result.content_id is not None
        assert len(result.content_id) == 64  # BLAKE3 hex

    def test_cas_write_content_readable(self, engine_with_cas):
        """Content written via sys_write is readable via sys_read."""
        engine, _ = engine_with_cas
        content = b"roundtrip content"
        engine.dcache_put("/workspace/rt.txt", "local", "", 0, DT_REG, etag="old")

        write_result = engine.sys_write("/workspace/rt.txt", "root", content, False)
        assert write_result.hit is True

        # Update dcache with new etag
        engine.dcache_put(
            "/workspace/rt.txt",
            "local",
            write_result.content_id,
            len(content),
            DT_REG,
            etag=write_result.content_id,
        )

        read_result = engine.sys_read("/workspace/rt.txt", "root", False)
        assert read_result.hit is True
        assert read_result.data == content

    def test_dcache_miss_raises_not_found(self, engine_with_cas):
        """DCache is authoritative — miss raises NexusFileNotFoundError."""
        import pytest

        from nexus.contracts.exceptions import NexusFileNotFoundError

        engine, _ = engine_with_cas
        with pytest.raises(NexusFileNotFoundError):
            engine.sys_read("/workspace/missing.txt", "root", False)

    def test_no_etag_returns_miss(self, engine_with_cas):
        """Entry without etag -> hit=false (no content hash to read)."""
        engine, _ = engine_with_cas
        engine.dcache_put("/workspace/new.txt", "local", "", 0, DT_REG)
        result = engine.sys_read("/workspace/new.txt", "root", False)
        assert result.hit is False


# ---------------------------------------------------------------------------
# No backend registered -> Python fallback
# ---------------------------------------------------------------------------


class TestNoBackendFallback:
    def test_no_backend_read_returns_miss(self):
        """Mount without local_root (no Rust backend) -> hit=false."""
        kernel = Kernel()
        kernel.add_mount("/", "root", False, False, "balanced")
        kernel.dcache_put("/workspace/test.txt", "remote", "test.txt", 100, DT_REG, etag="hash")

        result = kernel.sys_read("/workspace/test.txt", "root", False)
        assert result.hit is False
        assert result.data is None

    def test_no_backend_write_returns_miss(self):
        """Mount without local_root -> write hit=false."""
        kernel = Kernel()
        kernel.add_mount("/", "root", False, False, "balanced")
        kernel.dcache_put("/workspace/test.txt", "remote", "test.txt", 100, DT_REG, etag="hash")

        result = kernel.sys_write("/workspace/test.txt", "root", b"data", False)
        assert result.hit is False
        assert result.content_id is None
