"""Tests for SyscallEngine Phase H — sys_stat, plan_stat, plan_unlink, plan_rename.

Verifies:
1. sys_stat returns dict on dcache hit (full Rust path)
2. sys_stat returns None on miss, hooks, virtual paths
3. plan_stat returns dcache metadata
4. plan_unlink delegates to plan_write
5. plan_rename validates + routes both paths
6. Hook counts for stat/delete/rename
7. DCache mime_type field
"""

import pytest
from nexus_fast import (
    PathTrie,
    RenamePlan,
    RustDCache,
    RustPathRouter,
    StatPlan,
    SyscallEngine,
    VFSLockManager,
    WritePlan,
)

# ── Action constants (mirror syscall.rs) ──────────────────────────────

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
    """Create shared components for SyscallEngine."""
    dcache = RustDCache()
    router = RustPathRouter()
    trie = PathTrie()
    vfs_lock = VFSLockManager()
    router.add_mount("/", "root", False, False, "balanced")
    return dcache, router, trie, vfs_lock


@pytest.fixture
def engine(components):
    """Create SyscallEngine with VFS lock."""
    dcache, router, trie, vfs_lock = components
    return SyscallEngine(dcache, router, trie, vfs_lock)


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
        engine = SyscallEngine(dcache, router, trie, vfs_lock)
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
        engine = SyscallEngine(dcache, router, trie, vfs_lock)
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
        engine = SyscallEngine(dcache, router, trie, vfs_lock)
        result = engine.sys_stat("/zone/proc/123/status", "root", False)
        assert result is None

    def test_hook_bypass(self, components):
        """Stat hooks present → return None."""
        dcache, router, trie, vfs_lock = components
        dcache.put("/workspace/test.txt", "local", "test.txt", 100, DT_REG)
        engine = SyscallEngine(dcache, router, trie, vfs_lock)
        engine.set_hook_count("stat", 1)
        result = engine.sys_stat("/workspace/test.txt", "root", False)
        assert result is None

    def test_default_mime_type_file(self, components):
        """File without mime_type gets application/octet-stream."""
        dcache, router, trie, vfs_lock = components
        dcache.put("/workspace/data.bin", "local", "data.bin", 512, DT_REG)
        engine = SyscallEngine(dcache, router, trie, vfs_lock)
        result = engine.sys_stat("/workspace/data.bin", "root", False)
        assert result is not None
        assert result["mime_type"] == "application/octet-stream"

    def test_default_mime_type_directory(self, components):
        """Directory without mime_type gets inode/directory."""
        dcache, router, trie, vfs_lock = components
        dcache.put("/workspace/dir", "local", "", 0, DT_DIR)
        engine = SyscallEngine(dcache, router, trie, vfs_lock)
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
        engine = SyscallEngine(dcache, router, trie, vfs_lock)
        result = engine.sys_stat("/workspace/f.txt", "root", False)
        assert result is not None
        assert result["created_at"] is None
        assert result["modified_at"] is None


# ── plan_stat ────────────────────────────────────────────────────────


class TestPlanStat:
    def test_dcache_hit(self, components):
        """DCache hit returns full metadata."""
        dcache, router, trie, vfs_lock = components
        dcache.put(
            "/workspace/f.txt",
            "local",
            "f.txt",
            42,
            DT_REG,
            version=5,
            etag="etag1",
            zone_id="root",
            mime_type="text/plain",
        )
        engine = SyscallEngine(dcache, router, trie, vfs_lock)
        plan = engine.plan_stat("/workspace/f.txt", "root", False)
        assert isinstance(plan, StatPlan)
        assert plan.action == ACTION_DCACHE_HIT
        assert plan.backend_name == "local"
        assert plan.physical_path == "f.txt"
        assert plan.size == 42
        assert plan.etag == "etag1"
        assert plan.mime_type == "text/plain"
        assert plan.entry_type == DT_REG
        assert plan.version == 5
        assert plan.zone_id == "root"
        assert plan.is_directory is False

    def test_dcache_miss(self, engine):
        """DCache miss returns ACTION_CACHE_MISS."""
        plan = engine.plan_stat("/workspace/missing.txt", "root", False)
        assert plan.action == ACTION_CACHE_MISS

    def test_directory_flag(self, components):
        """Directory entry sets is_directory=True."""
        dcache, router, trie, vfs_lock = components
        dcache.put("/docs", "local", "", 0, DT_DIR)
        engine = SyscallEngine(dcache, router, trie, vfs_lock)
        plan = engine.plan_stat("/docs", "root", False)
        assert plan.action == ACTION_DCACHE_HIT
        assert plan.is_directory is True

    def test_resolved_virtual_path(self, components):
        """PathTrie match returns ACTION_RESOLVED."""
        dcache, router, trie, vfs_lock = components
        trie.register("/{}/proc/{}/status", 42)
        engine = SyscallEngine(dcache, router, trie, vfs_lock)
        plan = engine.plan_stat("/zone/proc/123/status", "root", False)
        assert plan.action == ACTION_RESOLVED
        assert plan.resolver_idx == 42


# ── plan_unlink ──────────────────────────────────────────────────────


class TestPlanUnlink:
    def test_dcache_hit(self, components):
        """Unlink plan on existing file."""
        dcache, router, trie, vfs_lock = components
        dcache.put("/workspace/del.txt", "local", "del.txt", 100, DT_REG, etag="hash")
        engine = SyscallEngine(dcache, router, trie, vfs_lock)
        plan = engine.plan_unlink("/workspace/del.txt", "root", False)
        assert isinstance(plan, WritePlan)
        assert plan.action == ACTION_DCACHE_HIT
        assert plan.backend_name == "local"

    def test_dcache_miss(self, engine):
        """Unlink plan on missing file returns cache miss."""
        plan = engine.plan_unlink("/workspace/missing.txt", "root", False)
        assert plan.action == ACTION_CACHE_MISS


# ── plan_rename ──────────────────────────────────────────────────────


class TestPlanRename:
    def test_both_paths_valid(self, components):
        """Rename plan validates and routes both paths."""
        dcache, router, trie, vfs_lock = components
        dcache.put("/workspace/old.txt", "local", "old.txt", 100, DT_REG)
        engine = SyscallEngine(dcache, router, trie, vfs_lock)
        plan = engine.plan_rename("/workspace/old.txt", "/workspace/new.txt", "root", False)
        assert isinstance(plan, RenamePlan)
        assert plan.action == ACTION_DCACHE_HIT
        assert plan.old_path == "/workspace/old.txt"
        assert plan.new_path == "/workspace/new.txt"
        assert "/root" in plan.old_mount_point  # Zone-canonical
        assert "/root" in plan.new_mount_point
        assert plan.entry_type == DT_REG

    def test_invalid_old_path(self, engine):
        """Invalid old path returns error."""
        plan = engine.plan_rename("", "/workspace/new.txt", "root", False)
        assert plan.action == ACTION_ERROR
        assert plan.error_msg is not None

    def test_invalid_new_path(self, engine):
        """Invalid new path returns error."""
        plan = engine.plan_rename("/workspace/old.txt", "", "root", False)
        assert plan.action == ACTION_ERROR

    def test_readonly_old_path(self, components):
        """Read-only source mount returns error."""
        dcache, router, trie, vfs_lock = components
        router.add_mount("/readonly", "root", True, False, "balanced")
        engine = SyscallEngine(dcache, router, trie, vfs_lock)
        plan = engine.plan_rename("/readonly/file.txt", "/workspace/new.txt", "root", False)
        assert plan.action == ACTION_ERROR

    def test_readonly_new_path(self, components):
        """Read-only destination mount returns error."""
        dcache, router, trie, vfs_lock = components
        router.add_mount("/readonly", "root", True, False, "balanced")
        engine = SyscallEngine(dcache, router, trie, vfs_lock)
        plan = engine.plan_rename("/workspace/old.txt", "/readonly/new.txt", "root", False)
        assert plan.action == ACTION_ERROR

    def test_source_dcache_miss_still_ok(self, engine):
        """Source not in dcache → entry_type is 0 but plan still succeeds."""
        plan = engine.plan_rename("/workspace/old.txt", "/workspace/new.txt", "root", False)
        assert plan.action == ACTION_DCACHE_HIT
        assert plan.entry_type == 0  # Unknown, Python checks existence


# ── Hook counts ──────────────────────────────────────────────────────


class TestHookCounts:
    def test_stat_hook_bypass(self, components):
        """Stat hooks bypass sys_stat."""
        dcache, router, trie, vfs_lock = components
        dcache.put("/workspace/f.txt", "local", "f.txt", 100, DT_REG)
        engine = SyscallEngine(dcache, router, trie, vfs_lock)
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
