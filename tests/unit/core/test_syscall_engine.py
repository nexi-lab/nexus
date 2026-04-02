"""Unit tests for SyscallEngine — single-FFI sys_read/sys_write planner.

Tests the PyO3 SyscallEngine, ReadPlan, and WritePlan classes.
"""

from __future__ import annotations

import unittest

try:
    from nexus_fast import (
        PathTrie,
        RustDCache,
        RustPathRouter,
        SyscallEngine,
    )

    RUST_AVAILABLE = True
except ImportError:
    RUST_AVAILABLE = False

# Action constants (must match syscall.rs)
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


def _make_engine(
    mounts: dict[str, tuple[bool, bool, str]] | None = None,
    entries: dict[str, tuple[str, str, int, int, str | None]] | None = None,
    patterns: dict[str, int] | None = None,
) -> SyscallEngine:
    """Helper to construct a SyscallEngine with test data.

    Args:
        mounts: {mount_point: (readonly, admin_only, io_profile)}
        entries: {path: (backend_name, physical_path, entry_type, version, etag)}
        patterns: {pattern: resolver_idx}
    """
    dcache = RustDCache()
    router = RustPathRouter()
    trie = PathTrie()

    if mounts:
        for mp, (ro, admin, profile) in mounts.items():
            router.add_mount(mp, "root", ro, admin, profile)

    if entries:
        for path, (bn, pp, et, ver, etag) in entries.items():
            dcache.put(path, bn, pp, 0, et, ver, etag)

    if patterns:
        for pat, idx in patterns.items():
            trie.register(pat, idx)

    return SyscallEngine(dcache, router, trie)


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_fast extension not available")
class TestSyscallEngineConstruction(unittest.TestCase):
    def test_construct(self) -> None:
        engine = _make_engine()
        assert engine is not None


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_fast extension not available")
class TestPlanReadDCacheHit(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = _make_engine(
            mounts={"/": (False, False, "balanced"), "/workspace": (False, False, "fast")},
            entries={
                "/workspace/file.txt": ("local", "/data/file.txt", DT_REG, 1, "abc123"),
                "/workspace/dir": ("local", "/data/dir", DT_DIR, 1, None),
            },
        )

    def test_dcache_hit_regular_file(self) -> None:
        plan = self.engine.plan_read("/workspace/file.txt", "root", False)
        assert plan.action == ACTION_DCACHE_HIT
        assert plan.backend_name == "local"
        assert plan.etag == "abc123"
        assert plan.entry_type == DT_REG
        assert plan.validated_path == "/workspace/file.txt"
        assert "workspace" in plan.mount_point
        assert plan.io_profile == "fast"
        assert plan.error_msg is None

    def test_dcache_hit_directory(self) -> None:
        plan = self.engine.plan_read("/workspace/dir", "root", False)
        assert plan.action == ACTION_DCACHE_HIT
        assert plan.entry_type == DT_DIR


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_fast extension not available")
class TestPlanReadCacheMiss(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = _make_engine(
            mounts={"/": (False, False, "balanced")},
        )

    def test_dcache_miss(self) -> None:
        plan = self.engine.plan_read("/unknown/file.txt", "root", False)
        assert plan.action == ACTION_CACHE_MISS
        assert plan.validated_path == "/unknown/file.txt"

    def test_no_mount(self) -> None:
        # No mounts at all
        engine = _make_engine()
        plan = engine.plan_read("/any/path", "root", False)
        assert plan.action == ACTION_CACHE_MISS


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_fast extension not available")
class TestPlanReadSpecialTypes(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = _make_engine(
            mounts={"/": (False, False, "balanced")},
            entries={
                "/ipc/pipe": ("pipe@local", "/ipc/pipe", DT_PIPE, 1, None),
                "/ipc/stream": ("stream@local", "/ipc/stream", DT_STREAM, 1, None),
                "/ext/file": ("gdrive", "/ext/file", DT_EXTERNAL, 1, None),
            },
        )

    def test_pipe(self) -> None:
        plan = self.engine.plan_read("/ipc/pipe", "root", False)
        assert plan.action == ACTION_PIPE
        assert plan.entry_type == DT_PIPE

    def test_stream(self) -> None:
        plan = self.engine.plan_read("/ipc/stream", "root", False)
        assert plan.action == ACTION_STREAM
        assert plan.entry_type == DT_STREAM

    def test_external(self) -> None:
        plan = self.engine.plan_read("/ext/file", "root", False)
        assert plan.action == ACTION_EXTERNAL
        assert plan.entry_type == DT_EXTERNAL


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_fast extension not available")
class TestPlanReadResolved(unittest.TestCase):
    def test_trie_resolver_match(self) -> None:
        engine = _make_engine(
            mounts={"/": (False, False, "balanced")},
            patterns={"/{}/proc/{}/status": 42},
        )
        plan = engine.plan_read("/root/proc/123/status", "root", False)
        assert plan.action == ACTION_RESOLVED
        assert plan.resolver_idx == 42
        assert plan.validated_path == "/root/proc/123/status"


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_fast extension not available")
class TestPlanReadValidation(unittest.TestCase):
    def test_empty_path(self) -> None:
        engine = _make_engine()
        plan = engine.plan_read("", "root", False)
        assert plan.action == ACTION_ERROR
        assert plan.error_msg is not None
        assert "empty" in plan.error_msg.lower()

    def test_no_leading_slash(self) -> None:
        engine = _make_engine()
        plan = engine.plan_read("no/slash", "root", False)
        assert plan.action == ACTION_ERROR

    def test_parent_traversal(self) -> None:
        engine = _make_engine()
        plan = engine.plan_read("/escape/../etc/passwd", "root", False)
        assert plan.action == ACTION_ERROR
        assert ".." in (plan.error_msg or "")


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_fast extension not available")
class TestPlanWrite(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = _make_engine(
            mounts={"/": (False, False, "balanced"), "/ro": (True, False, "balanced")},
            entries={
                "/docs/file.txt": ("local", "/data/file.txt", DT_REG, 3, "hash456"),
                "/ipc/pipe": ("pipe@local", "/ipc/pipe", DT_PIPE, 1, None),
            },
        )

    def test_dcache_hit(self) -> None:
        plan = self.engine.plan_write("/docs/file.txt", "root", False)
        assert plan.action == ACTION_DCACHE_HIT
        assert plan.etag == "hash456"
        assert plan.version == 3

    def test_pipe_write(self) -> None:
        plan = self.engine.plan_write("/ipc/pipe", "root", False)
        assert plan.action == ACTION_PIPE

    def test_cache_miss_write(self) -> None:
        plan = self.engine.plan_write("/new/file.txt", "root", False)
        assert plan.action == ACTION_CACHE_MISS

    def test_readonly_mount_write(self) -> None:
        # Write to readonly mount returns CACHE_MISS (router error → fallback)
        engine = _make_engine(
            mounts={"/ro": (True, False, "balanced")},
            entries={"/ro/file.txt": ("local", "/data/file.txt", DT_REG, 1, "abc")},
        )
        plan = engine.plan_write("/ro/file.txt", "root", False)
        # Router error (readonly) → falls back to cache_miss so Python can raise
        assert plan.action == ACTION_CACHE_MISS

    def test_validation_error(self) -> None:
        plan = self.engine.plan_write("", "root", False)
        assert plan.action == ACTION_ERROR

    def test_resolved_write(self) -> None:
        engine = _make_engine(
            mounts={"/": (False, False, "balanced")},
            patterns={"/{}/proc/{}/status": 7},
        )
        plan = engine.plan_write("/root/proc/123/status", "root", False)
        assert plan.action == ACTION_RESOLVED
        assert plan.resolver_idx == 7


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_fast extension not available")
class TestPlanReadAttributes(unittest.TestCase):
    """Verify all ReadPlan attributes are accessible."""

    def test_all_fields_accessible(self) -> None:
        engine = _make_engine(
            mounts={"/workspace": (False, False, "fast")},
            entries={"/workspace/f.txt": ("s3", "/bucket/f.txt", DT_REG, 2, "etag1")},
        )
        plan = engine.plan_read("/workspace/f.txt", "root", False)

        # All fields should be readable without error
        assert isinstance(plan.action, int)
        assert isinstance(plan.mount_point, str)
        assert isinstance(plan.backend_path, str)
        assert isinstance(plan.backend_name, str)
        assert isinstance(plan.readonly, bool)
        assert isinstance(plan.io_profile, str)
        assert isinstance(plan.entry_type, int)
        assert isinstance(plan.validated_path, str)
        assert isinstance(plan.resolver_idx, int)
        # etag can be str or None
        assert plan.etag is None or isinstance(plan.etag, str)
        # error_msg should be None for success
        assert plan.error_msg is None


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_fast extension not available")
class TestArcSharing(unittest.TestCase):
    """Verify that SyscallEngine shares state with the original objects."""

    def test_mutations_visible(self) -> None:
        dcache = RustDCache()
        router = RustPathRouter()
        trie = PathTrie()

        router.add_mount("/", "root", False, False, "balanced")
        engine = SyscallEngine(dcache, router, trie)

        # Before: cache miss
        plan = engine.plan_read("/test.txt", "root", False)
        assert plan.action == ACTION_CACHE_MISS

        # Mutate dcache after engine creation
        dcache.put("/test.txt", "local", "/data/test.txt", 100, DT_REG, 1, "etag-new")

        # After: should see the new entry (Arc sharing)
        plan = engine.plan_read("/test.txt", "root", False)
        assert plan.action == ACTION_DCACHE_HIT
        assert plan.etag == "etag-new"

    def test_router_mutations_visible(self) -> None:
        dcache = RustDCache()
        router = RustPathRouter()
        trie = PathTrie()

        engine = SyscallEngine(dcache, router, trie)

        # Before: no mount → cache miss
        dcache.put("/test.txt", "local", "/data/test.txt", 100, DT_REG)
        plan = engine.plan_read("/test.txt", "root", False)
        assert plan.action == ACTION_CACHE_MISS

        # Add mount after engine creation
        router.add_mount("/", "root", False, False, "balanced")

        # After: mount exists → dcache hit
        plan = engine.plan_read("/test.txt", "root", False)
        assert plan.action == ACTION_DCACHE_HIT

    def test_trie_mutations_visible(self) -> None:
        dcache = RustDCache()
        router = RustPathRouter()
        trie = PathTrie()

        router.add_mount("/", "root", False, False, "balanced")
        engine = SyscallEngine(dcache, router, trie)

        # Before: no resolver → cache miss (or dcache hit depending on data)
        plan = engine.plan_read("/zone/proc/123/status", "root", False)
        assert plan.action != ACTION_RESOLVED

        # Register trie pattern after engine creation
        trie.register("/{}/proc/{}/status", 99)

        # After: should resolve
        plan = engine.plan_read("/zone/proc/123/status", "root", False)
        assert plan.action == ACTION_RESOLVED
        assert plan.resolver_idx == 99


if __name__ == "__main__":
    unittest.main()
