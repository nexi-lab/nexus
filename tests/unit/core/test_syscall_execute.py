"""Tests for SyscallEngine sys_read / sys_write (Phase E / E.1 / G).

Verifies the full Rust data path: validate → route → dcache → CAS read/write.

Phase E.1: CAS engines are now owned by MountEntry (via add_mount's local_root
parameter), not registered separately on SyscallEngine.
Phase G: renamed execute_read → sys_read, execute_write → sys_write.
"""

import tempfile
from pathlib import Path

import pytest
from nexus_fast import (
    PathTrie,
    ReadPlan,
    RustDCache,
    RustPathRouter,
    SyscallEngine,
    WritePlan,
    hash_bytes,
)

# ── Action constants (mirror syscall.rs) ──────────────────────────────

# Must match syscall.rs ACTION_* constants
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
def components():
    """Create shared components for SyscallEngine (no CAS)."""
    dcache = RustDCache()
    router = RustPathRouter()
    trie = PathTrie()
    # Add root mount without CAS
    router.add_mount("/", "root", False, False, "balanced")
    return dcache, router, trie


@pytest.fixture
def engine(components):
    """Create SyscallEngine without CAS backends."""
    dcache, router, trie = components
    return SyscallEngine(dcache, router, trie)


@pytest.fixture
def cas_dir():
    """Temporary directory for CAS storage."""
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def engine_with_cas(cas_dir):
    """Create SyscallEngine with local CAS on root mount (Phase E.1)."""
    dcache = RustDCache()
    router = RustPathRouter()
    trie = PathTrie()
    # Root mount with CAS backend
    router.add_mount("/", "root", False, False, "balanced", "local", cas_dir, False)
    engine = SyscallEngine(dcache, router, trie)
    return engine, dcache, cas_dir


# ── SyscallEngine construction ────────────────────────────────────────


class TestSyscallEngineConstruction:
    def test_basic_construction(self, components):
        dcache, router, trie = components
        engine = SyscallEngine(dcache, router, trie)
        assert engine is not None

    def test_repr(self, engine):
        r = repr(engine)
        assert "SyscallEngine" in r


# ── plan_read ─────────────────────────────────────────────────────────


class TestPlanRead:
    def test_dcache_hit(self, components):
        dcache, router, trie = components
        engine = SyscallEngine(dcache, router, trie)
        dcache.put("/workspace/test.txt", "local", "test.txt", 100, DT_REG, etag="hash123")
        plan = engine.plan_read("/workspace/test.txt", "root", False)
        assert plan.action == ACTION_DCACHE_HIT
        assert plan.etag == "hash123"
        assert plan.backend_name == "local"
        assert plan.validated_path == "/workspace/test.txt"

    def test_dcache_miss(self, engine):
        plan = engine.plan_read("/workspace/missing.txt", "root", False)
        assert plan.action == ACTION_CACHE_MISS

    def test_resolved_virtual_path(self, components):
        dcache, router, trie = components
        trie.register("/{}/proc/{}/status", 42)
        engine = SyscallEngine(dcache, router, trie)
        plan = engine.plan_read("/zone/proc/123/status", "root", False)
        assert plan.action == ACTION_RESOLVED
        assert plan.resolver_idx == 42

    def test_pipe_entry(self, components):
        dcache, router, trie = components
        dcache.put("/pipes/fifo1", "local", "", 0, DT_PIPE)
        engine = SyscallEngine(dcache, router, trie)
        plan = engine.plan_read("/pipes/fifo1", "root", False)
        assert plan.action == ACTION_PIPE

    def test_stream_entry(self, components):
        dcache, router, trie = components
        dcache.put("/streams/s1", "local", "", 0, DT_STREAM)
        engine = SyscallEngine(dcache, router, trie)
        plan = engine.plan_read("/streams/s1", "root", False)
        assert plan.action == ACTION_STREAM

    def test_validation_error_empty(self, engine):
        plan = engine.plan_read("", "root", False)
        assert plan.action == ACTION_ERROR
        assert plan.error_msg is not None

    def test_root_path_returns_cache_miss(self, engine):
        plan = engine.plan_read("/", "root", False)
        # Root "/" is valid but typically not in dcache → cache miss
        assert plan.action in (ACTION_DCACHE_HIT, ACTION_CACHE_MISS)


# ── plan_write ────────────────────────────────────────────────────────


class TestPlanWrite:
    def test_dcache_hit(self, components):
        dcache, router, trie = components
        dcache.put("/workspace/test.txt", "local", "test.txt", 100, DT_REG, etag="hash")
        engine = SyscallEngine(dcache, router, trie)
        plan = engine.plan_write("/workspace/test.txt", "root", False)
        assert plan.action == ACTION_DCACHE_HIT
        assert plan.backend_name == "local"

    def test_readonly_mount_returns_cache_miss(self, components):
        dcache, router, trie = components
        router.add_mount("/readonly", "root", True, False, "balanced")
        engine = SyscallEngine(dcache, router, trie)
        plan = engine.plan_write("/readonly/file.txt", "root", False)
        # PR B returns ACTION_CACHE_MISS on route failure (Python handles error)
        assert plan.action == ACTION_CACHE_MISS


# ── execute_read ──────────────────────────────────────────────────────


class TestExecuteRead:
    def test_full_rust_path(self, engine_with_cas):
        """DCached hit + local CAS → bytes returned entirely from Rust."""
        engine, dcache, cas_dir = engine_with_cas
        content = b"hello from Rust execute_read"
        content_hash = hash_bytes(content)

        # Write CAS blob manually
        cas_path = Path(cas_dir) / "cas" / content_hash[:2] / content_hash[2:4] / content_hash
        cas_path.parent.mkdir(parents=True, exist_ok=True)
        cas_path.write_bytes(content)

        # Put in dcache
        dcache.put(
            "/workspace/test.txt", "local", content_hash, len(content), DT_REG, etag=content_hash
        )

        # Execute read
        result = engine.sys_read("/workspace/test.txt", "root", False)
        assert result is not None
        assert result == content

    def test_dcache_miss_returns_none(self, engine_with_cas):
        """DCache miss → None (Python fallback)."""
        engine, _, _ = engine_with_cas
        result = engine.sys_read("/workspace/missing.txt", "root", False)
        assert result is None

    def test_no_cas_backend_returns_none(self, components):
        """Mount without CAS (no local_root) → None (Python fallback)."""
        dcache, router, trie = components
        dcache.put("/workspace/test.txt", "s3-backend", "test.txt", 100, DT_REG, etag="hash123")
        engine = SyscallEngine(dcache, router, trie)
        result = engine.sys_read("/workspace/test.txt", "root", False)
        assert result is None

    def test_no_etag_returns_none(self, engine_with_cas):
        """Entry without etag (e.g. new file) → None."""
        engine, dcache, _ = engine_with_cas
        dcache.put("/workspace/new.txt", "local", "", 0, DT_REG)
        result = engine.sys_read("/workspace/new.txt", "root", False)
        assert result is None

    def test_pipe_entry_returns_none(self, engine_with_cas):
        """DT_PIPE entries are handled by Python PipeManager."""
        engine, dcache, _ = engine_with_cas
        dcache.put("/pipes/fifo", "local", "", 0, DT_PIPE)
        result = engine.sys_read("/pipes/fifo", "root", False)
        assert result is None

    def test_cas_read_failure_returns_none(self, engine_with_cas):
        """CAS blob not on disk → None (Python fallback)."""
        engine, dcache, _ = engine_with_cas
        dcache.put(
            "/workspace/test.txt",
            "local",
            "test.txt",
            100,
            DT_REG,
            etag="nonexistenthash0000000000000000000000000000000000000000000000",
        )
        result = engine.sys_read("/workspace/test.txt", "root", False)
        assert result is None

    def test_large_file(self, engine_with_cas):
        """Verify large file read works."""
        engine, dcache, cas_dir = engine_with_cas
        content = b"x" * (512 * 1024)  # 512KB
        content_hash = hash_bytes(content)

        cas_path = Path(cas_dir) / "cas" / content_hash[:2] / content_hash[2:4] / content_hash
        cas_path.parent.mkdir(parents=True, exist_ok=True)
        cas_path.write_bytes(content)

        dcache.put(
            "/workspace/big.bin", "local", content_hash, len(content), DT_REG, etag=content_hash
        )
        result = engine.sys_read("/workspace/big.bin", "root", False)
        assert result is not None
        assert len(result) == 512 * 1024


# ── execute_write ─────────────────────────────────────────────────────


class TestExecuteWrite:
    def test_full_rust_write(self, engine_with_cas):
        """DCache hit + local CAS → content hash returned."""
        engine, dcache, cas_dir = engine_with_cas
        dcache.put("/workspace/test.txt", "local", "", 0, DT_REG, etag="oldhash")

        content = b"new content from Rust execute_write"
        result = engine.sys_write("/workspace/test.txt", "root", content, False)
        assert result is not None
        assert len(result) == 64  # BLAKE3 hex

        # Verify content was actually written to CAS
        cas_path = Path(cas_dir) / "cas" / result[:2] / result[2:4] / result
        assert cas_path.exists()
        assert cas_path.read_bytes() == content

    def test_dcache_miss_returns_none(self, engine_with_cas):
        """DCache miss → None (Python handles new file)."""
        engine, _, _ = engine_with_cas
        result = engine.sys_write("/workspace/new.txt", "root", b"data", False)
        assert result is None

    def test_no_cas_backend_returns_none(self, components):
        """Mount without CAS → None."""
        dcache, router, trie = components
        dcache.put("/workspace/test.txt", "s3-remote", "", 0, DT_REG, etag="hash")
        engine = SyscallEngine(dcache, router, trie)
        result = engine.sys_write("/workspace/test.txt", "root", b"data", False)
        assert result is None

    def test_readonly_returns_none(self, cas_dir):
        """Write to readonly mount → None (Python handles error)."""
        dcache = RustDCache()
        router = RustPathRouter()
        trie = PathTrie()
        router.add_mount("/", "root", False, False, "balanced")
        router.add_mount("/readonly", "root", True, False, "balanced", "local", cas_dir, False)
        dcache.put("/readonly/file.txt", "local", "", 0, DT_REG, etag="hash")
        engine = SyscallEngine(dcache, router, trie)
        # PR B returns cache miss on route failure → execute_write returns None
        result = engine.sys_write("/readonly/file.txt", "root", b"data", False)
        assert result is None

    def test_write_dedup(self, engine_with_cas):
        """Writing same content twice returns same hash (CAS dedup)."""
        engine, dcache, _ = engine_with_cas
        dcache.put("/workspace/a.txt", "local", "", 0, DT_REG, etag="old1")
        dcache.put("/workspace/b.txt", "local", "", 0, DT_REG, etag="old2")

        content = b"deduplicated content"
        hash1 = engine.sys_write("/workspace/a.txt", "root", content, False)
        hash2 = engine.sys_write("/workspace/b.txt", "root", content, False)
        assert hash1 == hash2


# ── Arc sharing ───────────────────────────────────────────────────────


class TestArcSharing:
    def test_dcache_updates_visible(self, engine_with_cas):
        """DCache entries added after engine creation are visible."""
        engine, dcache, cas_dir = engine_with_cas
        content = b"added after init"
        content_hash = hash_bytes(content)

        # Write CAS blob
        cas_path = Path(cas_dir) / "cas" / content_hash[:2] / content_hash[2:4] / content_hash
        cas_path.parent.mkdir(parents=True, exist_ok=True)
        cas_path.write_bytes(content)

        # Initially miss
        assert engine.sys_read("/workspace/late.txt", "root", False) is None

        # Add to dcache (simulating metastore.put dual-write)
        dcache.put(
            "/workspace/late.txt", "local", content_hash, len(content), DT_REG, etag=content_hash
        )

        # Now should hit
        result = engine.sys_read("/workspace/late.txt", "root", False)
        assert result == content

    def test_mount_updates_visible(self, cas_dir):
        """Mounts added after engine creation are visible."""
        dcache = RustDCache()
        router = RustPathRouter()
        trie = PathTrie()
        router.add_mount("/", "root", False, False, "balanced")
        engine = SyscallEngine(dcache, router, trie)

        content = b"mount test"
        content_hash = hash_bytes(content)
        cas_path = Path(cas_dir) / "cas" / content_hash[:2] / content_hash[2:4] / content_hash
        cas_path.parent.mkdir(parents=True, exist_ok=True)
        cas_path.write_bytes(content)

        # Add mount for /workspace with CAS
        router.add_mount("/workspace", "root", False, False, "fast", "local", cas_dir, False)
        dcache.put(
            "/workspace/file.txt", "local", content_hash, len(content), DT_REG, etag=content_hash
        )

        plan = engine.plan_read("/workspace/file.txt", "root", False)
        assert plan.action == ACTION_DCACHE_HIT
        assert plan.io_profile == "fast"  # Uses /workspace mount, not /

        result = engine.sys_read("/workspace/file.txt", "root", False)
        assert result == content


# ── ReadPlan / WritePlan types ────────────────────────────────────────


class TestPlanTypes:
    def test_read_plan_fields(self, components):
        dcache, router, trie = components
        dcache.put("/workspace/f.txt", "local", "f.txt", 42, DT_REG, etag="etag1", zone_id="root")
        engine = SyscallEngine(dcache, router, trie)
        plan = engine.plan_read("/workspace/f.txt", "root", False)
        assert isinstance(plan, ReadPlan)
        assert plan.action == ACTION_DCACHE_HIT
        assert plan.etag == "etag1"
        assert plan.backend_name == "local"
        assert plan.entry_type == DT_REG
        assert plan.readonly is False
        assert plan.error_msg is None

    def test_write_plan_fields(self, components):
        dcache, router, trie = components
        dcache.put("/workspace/f.txt", "local", "f.txt", 42, DT_REG, etag="etag1")
        engine = SyscallEngine(dcache, router, trie)
        plan = engine.plan_write("/workspace/f.txt", "root", False)
        assert isinstance(plan, WritePlan)
        assert plan.action == ACTION_DCACHE_HIT
        assert plan.backend_name == "local"
