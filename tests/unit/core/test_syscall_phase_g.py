"""Tests for Phase G: Hook count + VFS lock integration in Kernel.

Verifies:
- Hook count > 0 -> sys_read/sys_write returns hit=false (Python handles hooks)
- VFS lock is acquired/released around I/O
- VFS lock contention -> returns hit=false (Python handles with blocking/timeout)
"""

import tempfile
from pathlib import Path

import pytest
from nexus_kernel import (
    Kernel,
    OperationContext,
    hash_bytes,
)

DT_REG = 0

# Test helper: default OperationContext for "root" zone
_ctx = OperationContext(zone_id="root")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cas_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def engine_with_lock(cas_dir):
    """Kernel with CAS backend."""
    kernel = Kernel()
    kernel.add_mount("/", "root", False, False, "balanced", "local", cas_dir, False)
    return kernel, cas_dir


# ---------------------------------------------------------------------------
# Hook count tests
# ---------------------------------------------------------------------------


class TestHookCountBypass:
    def test_read_hooks_set_post_hook_needed(self, engine_with_lock):
        """When read hooks are registered, sys_read returns post_hook_needed=true.

        PR 4: hooks no longer bypass the kernel. The kernel always reads.
        post_hook_needed flag tells the Python wrapper to fire post-hooks.
        """
        engine, cas_dir = engine_with_lock
        content = b"hook test data"
        content_hash = hash_bytes(content)
        cas_path = Path(cas_dir) / "cas" / content_hash[:2] / content_hash[2:4] / content_hash
        cas_path.parent.mkdir(parents=True, exist_ok=True)
        cas_path.write_bytes(content)
        engine.dcache_put(
            "/workspace/f.txt", "local", content_hash, len(content), DT_REG, etag=content_hash
        )

        # Without hooks: hit=true, post_hook_needed=false
        result = engine.sys_read("/workspace/f.txt", _ctx)
        assert result.hit is True
        assert result.post_hook_needed is False

        # Set read hook count > 0: hit=true, post_hook_needed=true
        engine.set_hook_count("read", 1)
        result = engine.sys_read("/workspace/f.txt", _ctx)
        assert result.hit is True
        assert result.post_hook_needed is True
        assert result.data == content

        # Reset hook count
        engine.set_hook_count("read", 0)
        result = engine.sys_read("/workspace/f.txt", _ctx)
        assert result.hit is True
        assert result.post_hook_needed is False

    def test_write_hooks_set_post_hook_needed(self, engine_with_lock):
        """When write hooks are registered, sys_write returns post_hook_needed=true.

        PR 5: hooks no longer bypass the kernel. The kernel always writes.
        post_hook_needed flag tells the Python wrapper to fire post-hooks.
        """
        engine, _ = engine_with_lock
        engine.dcache_put("/workspace/f.txt", "local", "", 0, DT_REG, etag="old")

        # Without hooks: hit=true, post_hook_needed=false
        result = engine.sys_write("/workspace/f.txt", _ctx, b"data")
        assert result.hit is True
        assert result.post_hook_needed is False

        # Set write hook count > 0: hit=true, post_hook_needed=true
        engine.set_hook_count("write", 1)
        result = engine.sys_write("/workspace/f.txt", _ctx, b"data")
        assert result.hit is True
        assert result.post_hook_needed is True

    def test_set_hook_count_unknown_op_ignored(self, engine_with_lock):
        """Unknown operation names are silently ignored."""
        engine, _ = engine_with_lock
        engine.set_hook_count("delete", 5)  # No crash


# ---------------------------------------------------------------------------
# VFS lock integration tests
# ---------------------------------------------------------------------------


class TestVFSLockIntegration:
    def test_read_with_lock(self, engine_with_lock):
        """sys_read acquires and releases VFS read lock."""
        engine, cas_dir = engine_with_lock
        content = b"locked read"
        content_hash = hash_bytes(content)
        cas_path = Path(cas_dir) / "cas" / content_hash[:2] / content_hash[2:4] / content_hash
        cas_path.parent.mkdir(parents=True, exist_ok=True)
        cas_path.write_bytes(content)
        engine.dcache_put(
            "/workspace/f.txt", "local", content_hash, len(content), DT_REG, etag=content_hash
        )

        result = engine.sys_read("/workspace/f.txt", _ctx)
        assert result.hit is True
        assert result.data == content

    def test_no_lock_manager_still_works(self):
        """Kernel without VFS lock manager still works."""
        kernel = Kernel()
        kernel.add_mount("/", "root", False, False, "balanced")
        kernel.dcache_put("/workspace/f.txt", "remote", "", 0, DT_REG, etag="hash")

        # Should not crash -- returns hit=false (no Rust backend)
        result = kernel.sys_read("/workspace/f.txt", _ctx)
        assert result.hit is False


# ---------------------------------------------------------------------------
# Backend callback with VFS lock
# ---------------------------------------------------------------------------


class TestCASBackendWithLock:
    def test_cas_read_with_lock(self):
        """CAS backend read works with kernel-internal lock manager."""
        import tempfile
        from pathlib import Path

        from nexus_kernel import hash_bytes

        with tempfile.TemporaryDirectory() as cas_dir:
            kernel = Kernel()
            kernel.add_mount("/", "root", False, False, "balanced", "local", cas_dir, False)

            content = b"locked cas data"
            content_hash = hash_bytes(content)
            cas_path = Path(cas_dir) / "cas" / content_hash[:2] / content_hash[2:4] / content_hash
            cas_path.parent.mkdir(parents=True, exist_ok=True)
            cas_path.write_bytes(content)

            kernel.dcache_put(
                "/workspace/f.txt", "local", content_hash, len(content), DT_REG, etag=content_hash
            )

            result = kernel.sys_read("/workspace/f.txt", _ctx)
            assert result.hit is True
            assert result.data == content
