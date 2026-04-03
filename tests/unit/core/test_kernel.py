"""Unit tests for Kernel — single-FFI syscall executor.

Tests the PyO3 Kernel class: construction, Arc sharing, and sys_* methods.
Plan classes (ReadPlan, WritePlan, StatPlan, RenamePlan) are kernel-internal
and no longer exposed to Python.
"""

from __future__ import annotations

import unittest

try:
    from nexus_fast import (
        Kernel,
        PathTrie,
        RustDCache,
        RustPathRouter,
    )

    RUST_AVAILABLE = True
except ImportError:
    RUST_AVAILABLE = False

# Entry type constants
DT_REG = 0
DT_DIR = 1
DT_MOUNT = 2
DT_PIPE = 3
DT_STREAM = 4
DT_EXTERNAL = 5


def _make_kernel(
    mounts: dict[str, tuple[bool, bool, str]] | None = None,
    entries: dict[str, tuple[str, str, int, int, str | None]] | None = None,
    patterns: dict[str, int] | None = None,
) -> Kernel:
    """Helper to construct a Kernel with test data.

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

    return Kernel(dcache, router, trie)


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_fast extension not available")
class TestKernelConstruction(unittest.TestCase):
    def test_construct(self) -> None:
        kernel = _make_kernel()
        assert kernel is not None


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_fast extension not available")
class TestArcSharing(unittest.TestCase):
    """Verify that Kernel shares state with the original objects."""

    def test_dcache_mutations_visible(self) -> None:
        """DCache entries added after Kernel creation are visible via sys_stat."""
        dcache = RustDCache()
        router = RustPathRouter()
        trie = PathTrie()

        router.add_mount("/", "root", False, False, "balanced")
        kernel = Kernel(dcache, router, trie)

        # Before: cache miss
        assert kernel.sys_stat("/test.txt", "root", False) is None

        # Mutate dcache after kernel creation
        dcache.put("/test.txt", "local", "/data/test.txt", 100, DT_REG, 1, "etag-new")

        # After: should see the new entry (Arc sharing)
        result = kernel.sys_stat("/test.txt", "root", False)
        assert result is not None
        assert result["etag"] == "etag-new"

    def test_router_mutations_visible(self) -> None:
        """Mounts added after Kernel creation are visible via sys_stat."""
        dcache = RustDCache()
        router = RustPathRouter()
        trie = PathTrie()

        kernel = Kernel(dcache, router, trie)

        # Before: no mount → None
        dcache.put("/test.txt", "local", "/data/test.txt", 100, DT_REG)
        assert kernel.sys_stat("/test.txt", "root", False) is None

        # Add mount after kernel creation
        router.add_mount("/", "root", False, False, "balanced")

        # After: mount exists → sys_stat returns result
        result = kernel.sys_stat("/test.txt", "root", False)
        assert result is not None

    def test_trie_mutations_visible(self) -> None:
        """PathTrie patterns added after Kernel creation are visible."""
        dcache = RustDCache()
        router = RustPathRouter()
        trie = PathTrie()

        router.add_mount("/", "root", False, False, "balanced")
        dcache.put("/zone/proc/123/status", "local", "status", 100, DT_REG)
        kernel = Kernel(dcache, router, trie)

        # Before: no resolver → sys_stat returns dcache hit
        result = kernel.sys_stat("/zone/proc/123/status", "root", False)
        assert result is not None

        # Register trie pattern after kernel creation
        trie.register("/{}/proc/{}/status", 99)

        # After: resolver matches → sys_stat returns None (virtual path)
        result = kernel.sys_stat("/zone/proc/123/status", "root", False)
        assert result is None


if __name__ == "__main__":
    unittest.main()
