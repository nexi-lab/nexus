"""Tests for Kernel Phase H — sys_stat and hook counts.

Verifies:
1. sys_stat returns dict on dcache hit (full Rust path)
2. sys_stat returns None on miss, hooks, virtual paths
3. Hook counts for stat/delete/rename
4. DCache mime_type field

Plan classes (StatPlan, WritePlan, RenamePlan) are kernel-internal
and no longer exposed to Python.
"""

import pytest
from nexus_fast import (
    Kernel,
    PathTrie,
    RustDCache,
    RustPathRouter,
    VFSLockManager,
)

# ── Action constants (mirror kernel.rs) ──────────────────────────────

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
DT_MOUNT = 2
DT_PIPE = 3
DT_STREAM = 4
DT_EXTERNAL = 5


@pytest.fixture
def components():
    """Create shared components for Kernel."""
    dcache = RustDCache()
    router = RustPathRouter()
    trie = PathTrie()
    vfs_lock = VFSLockManager()
    router.add_mount("/", "root", False, False, "balanced")
    return dcache, router, trie, vfs_lock


@pytest.fixture
def engine(components):
    """Create Kernel with VFS lock."""
    dcache, router, trie, vfs_lock = components
    return Kernel(dcache, router, trie, vfs_lock)


# ── sys_stat ─────────────────────────────────────────────────────────


class TestSysStat:
    def test_dcache_hit_file(self, components):
        """DCache hit for regular file returns dict."""
        dcache, router, trie, vfs_lock = components
        dcache.put(
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
        engine = Kernel(dcache, router, trie, vfs_lock)
        result = engine.sys_stat("/workspace/test.txt", "root", False)
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

    def test_dcache_hit_directory(self, components):
        """DCache hit for directory returns dict with dir defaults."""
        dcache, router, trie, vfs_lock = components
        dcache.put(
            "/workspace/docs",
            "local",
            "",
            0,
            DT_DIR,
            version=1,
            mime_type="inode/directory",
        )
        engine = Kernel(dcache, router, trie, vfs_lock)
        result = engine.sys_stat("/workspace/docs", "root", False)
        assert result is not None
        assert result["is_directory"] is True
        assert result["mode"] == 0o755
        assert result["size"] == 4096  # dirs with size=0 get 4096
        assert result["mime_type"] == "inode/directory"

    def test_dcache_miss_returns_none(self, engine):
        """DCache miss returns None (Python fallback)."""
        result = engine.sys_stat("/workspace/missing.txt", "root", False)
        assert result is None

    def test_virtual_path_returns_none(self, components):
        """PathTrie resolver match returns None (Python handles)."""
        dcache, router, trie, vfs_lock = components
        trie.register("/{}/proc/{}/status", 42)
        engine = Kernel(dcache, router, trie, vfs_lock)
        result = engine.sys_stat("/zone/proc/123/status", "root", False)
        assert result is None

    def test_hook_bypass(self, components):
        """Stat hooks present → return None."""
        dcache, router, trie, vfs_lock = components
        dcache.put("/workspace/test.txt", "local", "test.txt", 100, DT_REG)
        engine = Kernel(dcache, router, trie, vfs_lock)
        engine.set_hook_count("stat", 1)
        result = engine.sys_stat("/workspace/test.txt", "root", False)
        assert result is None

    def test_default_mime_type_file(self, components):
        """File without mime_type gets application/octet-stream."""
        dcache, router, trie, vfs_lock = components
        dcache.put("/workspace/data.bin", "local", "data.bin", 512, DT_REG)
        engine = Kernel(dcache, router, trie, vfs_lock)
        result = engine.sys_stat("/workspace/data.bin", "root", False)
        assert result is not None
        assert result["mime_type"] == "application/octet-stream"

    def test_default_mime_type_directory(self, components):
        """Directory without mime_type gets inode/directory."""
        dcache, router, trie, vfs_lock = components
        dcache.put("/workspace/dir", "local", "", 0, DT_DIR)
        engine = Kernel(dcache, router, trie, vfs_lock)
        result = engine.sys_stat("/workspace/dir", "root", False)
        assert result is not None
        assert result["mime_type"] == "inode/directory"

    def test_invalid_path(self, engine):
        """Invalid path returns None."""
        assert engine.sys_stat("", "root", False) is None
        assert engine.sys_stat("no-slash", "root", False) is None

    def test_timestamps_are_none(self, components):
        """Timestamps are None from Rust (Python fills from FileMetadata)."""
        dcache, router, trie, vfs_lock = components
        dcache.put("/workspace/f.txt", "local", "f.txt", 100, DT_REG)
        engine = Kernel(dcache, router, trie, vfs_lock)
        result = engine.sys_stat("/workspace/f.txt", "root", False)
        assert result is not None
        assert result["created_at"] is None
        assert result["modified_at"] is None


# ── Hook counts ──────────────────────────────────────────────────────


class TestHookCounts:
    def test_stat_hook_bypass(self, components):
        """Stat hooks bypass sys_stat."""
        dcache, router, trie, vfs_lock = components
        dcache.put("/workspace/f.txt", "local", "f.txt", 100, DT_REG)
        engine = Kernel(dcache, router, trie, vfs_lock)
        # Without hooks → returns result
        assert engine.sys_stat("/workspace/f.txt", "root", False) is not None
        # With hooks → returns None
        engine.set_hook_count("stat", 1)
        assert engine.sys_stat("/workspace/f.txt", "root", False) is None
        # Clear hooks → returns result again
        engine.set_hook_count("stat", 0)
        assert engine.sys_stat("/workspace/f.txt", "root", False) is not None

    def test_hook_count_ops(self, engine):
        """set_hook_count accepts stat/delete/rename without error."""
        engine.set_hook_count("stat", 5)
        engine.set_hook_count("delete", 3)
        engine.set_hook_count("rename", 1)
        engine.set_hook_count("read", 0)
        engine.set_hook_count("write", 0)
        # No assertion needed — just verify no crash


# ── DCache mime_type ─────────────────────────────────────────────────


class TestDCacheMimeType:
    def test_put_get_with_mime_type(self):
        """RustDCache stores and returns mime_type."""
        dcache = RustDCache()
        dcache.put(
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
        full = dcache.get_full("/workspace/doc.md")
        assert full is not None
        assert full["mime_type"] == "text/markdown"

    def test_put_get_without_mime_type(self):
        """RustDCache returns None for unset mime_type."""
        dcache = RustDCache()
        dcache.put("/workspace/data.bin", "local", "data.bin", 256, DT_REG)
        full = dcache.get_full("/workspace/data.bin")
        assert full is not None
        assert full["mime_type"] is None
