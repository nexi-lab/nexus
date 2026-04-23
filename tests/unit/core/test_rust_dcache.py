"""Unit tests for Kernel DCache proxy methods (Rust DashMap-backed dentry cache).

Tests the Kernel dcache_* proxy methods exposed from kernel.rs.
Also verifies MetastoreABC dual-write integration.
"""

from __future__ import annotations

import unittest

try:
    from nexus_kernel import Kernel

    RUST_AVAILABLE = True
except ImportError:
    RUST_AVAILABLE = False


# ── Entry type constants (mirror metadata.proto) ──────────────────────────

DT_REG = 0
DT_DIR = 1
DT_MOUNT = 2
DT_PIPE = 3
DT_STREAM = 4
DT_EXTERNAL = 5


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_kernel extension not available")
class TestRustDCachePut(unittest.TestCase):
    def setUp(self) -> None:
        self.kernel = Kernel()

    def test_put_and_len(self) -> None:
        assert self.kernel.dcache_len() == 0
        self.kernel.dcache_put("/a", "local", "/data/a", 100, DT_REG)
        assert self.kernel.dcache_len() == 1

    def test_put_overwrite(self) -> None:
        self.kernel.dcache_put("/a", "local", "/data/a", 100, DT_REG)
        self.kernel.dcache_put("/a", "s3", "/bucket/a", 200, DT_REG, version=2, etag="abc")
        assert self.kernel.dcache_len() == 1
        result = self.kernel.dcache_get("/a")
        assert result is not None
        assert result[0] == "s3"  # backend_name
        assert result[1] == "/bucket/a"  # physical_path

    def test_put_all_entry_types(self) -> None:
        for et in (DT_REG, DT_DIR, DT_MOUNT, DT_PIPE, DT_STREAM, DT_EXTERNAL):
            self.kernel.dcache_put(f"/type/{et}", "local", f"/data/{et}", 0, et)
        assert self.kernel.dcache_len() == 6


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_kernel extension not available")
class TestRustDCacheGet(unittest.TestCase):
    def setUp(self) -> None:
        self.kernel = Kernel()
        self.kernel.dcache_put(
            "/docs/readme.md", "local", "/data/readme.md", 1024, DT_REG, etag="hash1"
        )

    def test_get_hit(self) -> None:
        result = self.kernel.dcache_get("/docs/readme.md")
        assert result is not None
        backend_name, physical_path, entry_type = result
        assert backend_name == "local"
        assert physical_path == "/data/readme.md"
        assert entry_type == DT_REG

    def test_get_miss(self) -> None:
        result = self.kernel.dcache_get("/nonexistent")
        assert result is None

    def test_get_full_hit(self) -> None:
        result = self.kernel.dcache_get_full("/docs/readme.md")
        assert result is not None
        assert result["backend_name"] == "local"
        assert result["physical_path"] == "/data/readme.md"
        assert result["size"] == 1024
        assert result["etag"] == "hash1"
        assert result["version"] == 1
        assert result["entry_type"] == DT_REG

    def test_get_full_miss(self) -> None:
        assert self.kernel.dcache_get_full("/nonexistent") is None

    def test_get_full_optional_fields(self) -> None:
        self.kernel.dcache_put("/minimal", "s3", "/bucket/min", 0, DT_DIR)
        result = self.kernel.dcache_get_full("/minimal")
        assert result is not None
        assert result["etag"] is None
        assert result["zone_id"] is None

    def test_get_full_with_zone_id(self) -> None:
        self.kernel.dcache_put("/zoned", "local", "/data/z", 512, DT_REG, zone_id="corp")
        result = self.kernel.dcache_get_full("/zoned")
        assert result is not None
        assert result["zone_id"] == "corp"


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_kernel extension not available")
class TestRustDCacheEvict(unittest.TestCase):
    def setUp(self) -> None:
        self.kernel = Kernel()
        for i in range(5):
            self.kernel.dcache_put(
                f"/docs/file{i}.md", "local", f"/data/file{i}.md", i * 100, DT_REG
            )
        self.kernel.dcache_put("/src/main.rs", "local", "/data/main.rs", 2048, DT_REG)

    def test_evict_existing(self) -> None:
        assert self.kernel.dcache_evict("/docs/file0.md") is True
        assert self.kernel.dcache_len() == 5

    def test_evict_nonexistent(self) -> None:
        assert self.kernel.dcache_evict("/nonexistent") is False
        assert self.kernel.dcache_len() == 6

    def test_evict_prefix(self) -> None:
        count = self.kernel.dcache_evict_prefix("/docs/")
        assert count == 5
        assert self.kernel.dcache_len() == 1
        assert self.kernel.dcache_contains("/src/main.rs")

    def test_evict_prefix_empty(self) -> None:
        count = self.kernel.dcache_evict_prefix("/nonexistent/")
        assert count == 0
        assert self.kernel.dcache_len() == 6


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_kernel extension not available")
class TestRustDCacheContains(unittest.TestCase):
    def test_contains(self) -> None:
        kernel = Kernel()
        kernel.dcache_put("/a", "local", "/a", 0, DT_REG)
        assert kernel.dcache_contains("/a") is True
        assert kernel.dcache_contains("/b") is False


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_kernel extension not available")
class TestRustDCacheStats(unittest.TestCase):
    def test_stats_empty(self) -> None:
        kernel = Kernel()
        stats = kernel.dcache_stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["size"] == 0
        assert stats["hit_rate"] == 0.0

    def test_stats_with_activity(self) -> None:
        kernel = Kernel()
        kernel.dcache_put("/a", "local", "/a", 0, DT_REG)
        kernel.dcache_get("/a")  # hit
        kernel.dcache_get("/a")  # hit
        kernel.dcache_get("/miss")  # miss

        stats = kernel.dcache_stats()
        assert stats["hits"] == 2
        assert stats["misses"] == 1
        assert stats["size"] == 1
        assert abs(stats["hit_rate"] - 2 / 3) < 0.01


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_kernel extension not available")
class TestRustDCacheClear(unittest.TestCase):
    def test_clear(self) -> None:
        kernel = Kernel()
        kernel.dcache_put("/a", "local", "/a", 0, DT_REG)
        kernel.dcache_put("/b", "local", "/b", 0, DT_REG)
        kernel.dcache_get("/a")  # hit counter
        assert kernel.dcache_len() == 2

        kernel.dcache_clear()
        assert kernel.dcache_len() == 0
        stats = kernel.dcache_stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 0


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_kernel extension not available")
class TestRustDCacheRepr(unittest.TestCase):
    def test_repr(self) -> None:
        kernel = Kernel()
        kernel.dcache_put("/a", "local", "/a", 0, DT_REG)
        r = repr(kernel)
        assert "Kernel" in r


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_kernel extension not available")
class TestRustDCacheMetastoreIntegration(unittest.TestCase):
    """Verify MetastoreABC dual-write keeps Python dict and Kernel dcache in sync."""

    def _make_store(self):
        """Create a minimal MetastoreABC implementation for testing."""
        from nexus.contracts.metadata import FileMetadata
        from nexus.core.metastore import MetastoreABC

        class InMemoryStore(MetastoreABC):
            def __init__(self):
                super().__init__()
                self._store: dict[str, FileMetadata] = {}

            def _get_raw(self, path):
                return self._store.get(path)

            def _put_raw(self, metadata):
                self._store[metadata.path] = metadata
                return None

            def _delete_raw(self, path):
                return self._store.pop(path, None)

            def _exists_raw(self, path):
                return path in self._store

            def _list_raw(self, prefix="", recursive=True, **kwargs):
                return [m for p, m in self._store.items() if p.startswith(prefix)]

            def close(self):
                pass

        store = InMemoryStore()
        store._kernel = Kernel()
        return store

    def test_get_populates_rust_dcache(self) -> None:
        from nexus.contracts.metadata import FileMetadata

        store = self._make_store()

        meta = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="/data/file.txt",
            size=512,
            entry_type=DT_REG,
            etag="abc",
        )
        store._store["/test/file.txt"] = meta

        # First get -- dcache miss -> populates both caches
        result = store.get("/test/file.txt")
        assert result is not None
        assert store._kernel.dcache_contains("/test/file.txt")

        rust_entry = store._kernel.dcache_get_full("/test/file.txt")
        assert rust_entry["backend_name"] == "local"
        assert rust_entry["etag"] == "abc"

    def test_put_syncs_rust_dcache(self) -> None:
        from nexus.contracts.metadata import FileMetadata

        store = self._make_store()

        meta = FileMetadata(
            path="/new/file.txt",
            backend_name="s3",
            physical_path="/bucket/file.txt",
            size=1024,
            entry_type=DT_REG,
            version=3,
        )
        store.put(meta)

        assert store._kernel.dcache_contains("/new/file.txt")
        rust_entry = store._kernel.dcache_get_full("/new/file.txt")
        assert rust_entry["backend_name"] == "s3"
        assert rust_entry["size"] == 1024
        assert rust_entry["version"] == 3

    def test_delete_evicts_rust_dcache(self) -> None:
        from nexus.contracts.metadata import FileMetadata

        store = self._make_store()

        meta = FileMetadata(
            path="/del/me.txt",
            backend_name="local",
            physical_path="/data/me.txt",
            size=0,
            entry_type=DT_REG,
        )
        store.put(meta)
        assert store._kernel.dcache_contains("/del/me.txt")

        store.delete("/del/me.txt")
        assert not store._kernel.dcache_contains("/del/me.txt")

    def test_dcache_evict_prefix_syncs(self) -> None:
        from nexus.contracts.metadata import FileMetadata

        store = self._make_store()

        for i in range(3):
            store.put(
                FileMetadata(
                    path=f"/mount/file{i}",
                    backend_name="local",
                    physical_path=f"/data/file{i}",
                    size=0,
                    entry_type=DT_REG,
                )
            )
        store.put(
            FileMetadata(
                path="/other/keep",
                backend_name="local",
                physical_path="/data/keep",
                size=0,
                entry_type=DT_REG,
            )
        )

        evicted = store.dcache_evict_prefix("/mount/")
        assert evicted == 3
        assert not store._kernel.dcache_contains("/mount/file0")
        assert store._kernel.dcache_contains("/other/keep")

    def test_list_populates_rust_dcache(self) -> None:
        from nexus.contracts.metadata import FileMetadata

        store = self._make_store()

        for i in range(3):
            store._store[f"/list/file{i}"] = FileMetadata(
                path=f"/list/file{i}",
                backend_name="local",
                physical_path=f"/data/file{i}",
                size=i * 100,
                entry_type=DT_REG,
            )

        results = store.list("/list/")
        assert len(results) == 3
        for i in range(3):
            assert store._kernel.dcache_contains(f"/list/file{i}")

    def test_put_batch_syncs_rust_dcache(self) -> None:
        from nexus.contracts.metadata import FileMetadata

        store = self._make_store()

        metas = [
            FileMetadata(
                path=f"/batch/{i}",
                backend_name="local",
                physical_path=f"/data/{i}",
                size=i,
                entry_type=DT_REG,
            )
            for i in range(5)
        ]
        store.put_batch(metas)

        assert store._kernel.dcache_len() == 5
        for i in range(5):
            assert store._kernel.dcache_contains(f"/batch/{i}")

    def test_cache_stats_includes_rust(self) -> None:
        store = self._make_store()

        stats = store.cache_stats
        assert "rust" in stats
        assert "hits" in stats["rust"]
        assert "misses" in stats["rust"]


if __name__ == "__main__":
    unittest.main()
