"""Tests for Kernel Phase H -- sys_stat and hook counts.

Verifies:
1. sys_stat returns dict on dcache hit (full Rust path)
2. sys_stat returns None on miss, hooks, virtual paths
3. Hook counts for stat/delete/rename
4. DCache mime_type field

Plan classes (StatPlan, WritePlan, RenamePlan) are kernel-internal
and no longer exposed to Python.
"""

import pytest
from nexus_kernel import (
    Kernel,
)

# Entry type constants
DT_REG = 0
DT_DIR = 1
DT_MOUNT = 2
DT_PIPE = 3
DT_STREAM = 4
DT_EXTERNAL = 5


@pytest.fixture
def kernel():
    """Create Kernel with root mount."""
    k = Kernel()
    k.add_mount("/", "root", False, False, "balanced")
    return k


# ── sys_stat ─────────────────────────────────────────────────────────


class TestSysStat:
    def test_dcache_hit_file(self):
        """DCache hit for regular file returns dict."""
        kernel = Kernel()
        kernel.add_mount("/", "root", False, False, "balanced")
        kernel.dcache_put(
            "/workspace/test.txt",
            "local",
            "test.txt",
            1024,
            DT_REG,
            version=3,
            etag="hash123",
            zone_id="root",
            mime_type="text/plain",
        )
        result = kernel.sys_stat("/workspace/test.txt", "root", False)
        assert result is not None
        assert result["path"] == "/workspace/test.txt"
        assert result["backend_name"] == "local"
        assert result["physical_path"] == "test.txt"
        assert result["size"] == 1024
        assert result["etag"] == "hash123"
        assert result["mime_type"] == "text/plain"
        assert result["is_directory"] is False
        assert result["entry_type"] == DT_REG
        assert result["mode"] == 0o644
        assert result["version"] == 3
        assert result["zone_id"] == "root"

    def test_dcache_hit_directory(self):
        """DCache hit for directory returns dict with dir defaults."""
        kernel = Kernel()
        kernel.add_mount("/", "root", False, False, "balanced")
        kernel.dcache_put(
            "/workspace/docs",
            "local",
            "",
            0,
            DT_DIR,
            version=1,
            mime_type="inode/directory",
        )
        result = kernel.sys_stat("/workspace/docs", "root", False)
        assert result is not None
        assert result["is_directory"] is True
        assert result["mode"] == 0o755
        assert result["size"] == 4096  # dirs with size=0 get 4096
        assert result["mime_type"] == "inode/directory"

    def test_dcache_miss_returns_none(self, kernel):
        """DCache miss returns None (Python fallback)."""
        result = kernel.sys_stat("/workspace/missing.txt", "root", False)
        assert result is None

    def test_virtual_path_returns_none(self):
        """PathTrie resolver match returns None (Python handles)."""
        kernel = Kernel()
        kernel.add_mount("/", "root", False, False, "balanced")
        kernel.trie_register("/{}/proc/{}/status", 42)
        result = kernel.sys_stat("/zone/proc/123/status", "root", False)
        assert result is None

    def test_hooks_no_longer_bypass(self):
        """Stat hooks no longer bypass kernel — always returns dcache result."""
        kernel = Kernel()
        kernel.add_mount("/", "root", False, False, "balanced")
        kernel.dcache_put("/workspace/test.txt", "local", "test.txt", 100, DT_REG)
        kernel.set_hook_count("stat", 1)
        result = kernel.sys_stat("/workspace/test.txt", "root", False)
        assert result is not None  # hooks handled by wrapper, not kernel

    def test_default_mime_type_file(self):
        """File without mime_type gets application/octet-stream."""
        kernel = Kernel()
        kernel.add_mount("/", "root", False, False, "balanced")
        kernel.dcache_put("/workspace/data.bin", "local", "data.bin", 512, DT_REG)
        result = kernel.sys_stat("/workspace/data.bin", "root", False)
        assert result is not None
        assert result["mime_type"] == "application/octet-stream"

    def test_default_mime_type_directory(self):
        """Directory without mime_type gets inode/directory."""
        kernel = Kernel()
        kernel.add_mount("/", "root", False, False, "balanced")
        kernel.dcache_put("/workspace/dir", "local", "", 0, DT_DIR)
        result = kernel.sys_stat("/workspace/dir", "root", False)
        assert result is not None
        assert result["mime_type"] == "inode/directory"

    def test_invalid_path(self, kernel):
        """Invalid path returns None."""
        assert kernel.sys_stat("", "root", False) is None
        assert kernel.sys_stat("no-slash", "root", False) is None

    def test_timestamps_are_none(self):
        """Timestamps are None from Rust (Python fills from FileMetadata)."""
        kernel = Kernel()
        kernel.add_mount("/", "root", False, False, "balanced")
        kernel.dcache_put("/workspace/f.txt", "local", "f.txt", 100, DT_REG)
        result = kernel.sys_stat("/workspace/f.txt", "root", False)
        assert result is not None
        assert result["created_at"] is None
        assert result["modified_at"] is None


# ── Hook counts ──────────────────────────────────────────────────────


class TestHookCounts:
    def test_stat_always_returns_dcache_hit(self):
        """Hooks no longer bypass sys_stat — kernel always returns dcache result."""
        kernel = Kernel()
        kernel.add_mount("/", "root", False, False, "balanced")
        kernel.dcache_put("/workspace/f.txt", "local", "f.txt", 100, DT_REG)
        # Without hooks -> returns result
        assert kernel.sys_stat("/workspace/f.txt", "root", False) is not None
        # With hooks -> still returns result (hooks handled by wrapper)
        kernel.set_hook_count("stat", 1)
        assert kernel.sys_stat("/workspace/f.txt", "root", False) is not None
        # Clear hooks -> still returns result
        kernel.set_hook_count("stat", 0)
        assert kernel.sys_stat("/workspace/f.txt", "root", False) is not None

    def test_hook_count_ops(self, kernel):
        """set_hook_count accepts stat/delete/rename without error."""
        kernel.set_hook_count("stat", 5)
        kernel.set_hook_count("delete", 3)
        kernel.set_hook_count("rename", 1)
        kernel.set_hook_count("read", 0)
        kernel.set_hook_count("write", 0)
        # No assertion needed -- just verify no crash


# ── DCache mime_type ─────────────────────────────────────────────────


class TestDCacheMimeType:
    def test_put_get_with_mime_type(self):
        """Kernel dcache stores and returns mime_type."""
        kernel = Kernel()
        kernel.dcache_put(
            "/workspace/doc.md",
            "local",
            "doc.md",
            512,
            DT_REG,
            version=1,
            etag="hash1",
            zone_id="root",
            mime_type="text/markdown",
        )
        full = kernel.dcache_get_full("/workspace/doc.md")
        assert full is not None
        assert full["mime_type"] == "text/markdown"

    def test_put_get_without_mime_type(self):
        """Kernel dcache returns None for unset mime_type."""
        kernel = Kernel()
        kernel.dcache_put("/workspace/data.bin", "local", "data.bin", 256, DT_REG)
        full = kernel.dcache_get_full("/workspace/data.bin")
        assert full is not None
        assert full["mime_type"] is None
