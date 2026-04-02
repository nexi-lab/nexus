"""Unit tests for RustDCache (Rust DashMap-backed dentry cache).

Tests the PyO3 RustDCache class exposed from rust/nexus_pyo3/src/dcache.rs.
Also verifies MetastoreABC dual-write integration.
"""

from __future__ import annotations

import unittest

try:
    from nexus_fast import RustDCache

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


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_fast extension not available")
class TestRustDCachePut(unittest.TestCase):
    def setUp(self) -> None:
        self.dc = RustDCache()

    def test_put_and_len(self) -> None:
        assert len(self.dc) == 0
        self.dc.put("/a", "local", "/data/a", 100, DT_REG)
        assert len(self.dc) == 1

    def test_put_overwrite(self) -> None:
        self.dc.put("/a", "local", "/data/a", 100, DT_REG)
        self.dc.put("/a", "s3", "/bucket/a", 200, DT_REG, version=2, etag="abc")
        assert len(self.dc) == 1
        result = self.dc.get("/a")
        assert result is not None
        assert result[0] == "s3"  # backend_name
        assert result[1] == "/bucket/a"  # physical_path

    def test_put_all_entry_types(self) -> None:
        for et in (DT_REG, DT_DIR, DT_MOUNT, DT_PIPE, DT_STREAM, DT_EXTERNAL):
            self.dc.put(f"/type/{et}", "local", f"/data/{et}", 0, et)
        assert len(self.dc) == 6


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_fast extension not available")
class TestRustDCacheGet(unittest.TestCase):
    def setUp(self) -> None:
        self.dc = RustDCache()
        self.dc.put("/docs/readme.md", "local", "/data/readme.md", 1024, DT_REG, etag="hash1")

    def test_get_hit(self) -> None:
        result = self.dc.get("/docs/readme.md")
        assert result is not None
        backend_name, physical_path, entry_type = result
        assert backend_name == "local"
        assert physical_path == "/data/readme.md"
        assert entry_type == DT_REG

    def test_get_miss(self) -> None:
        result = self.dc.get("/nonexistent")
        assert result is None

    def test_get_full_hit(self) -> None:
        result = self.dc.get_full("/docs/readme.md")
        assert result is not None
        assert result["backend_name"] == "local"
        assert result["physical_path"] == "/data/readme.md"
        assert result["size"] == 1024
        assert result["etag"] == "hash1"
        assert result["version"] == 1
        assert result["entry_type"] == DT_REG

    def test_get_full_miss(self) -> None:
        assert self.dc.get_full("/nonexistent") is None

    def test_get_full_optional_fields(self) -> None:
        self.dc.put("/minimal", "s3", "/bucket/min", 0, DT_DIR)
        result = self.dc.get_full("/minimal")
        assert result is not None
        assert result["etag"] is None
        assert result["zone_id"] is None

    def test_get_full_with_zone_id(self) -> None:
        self.dc.put("/zoned", "local", "/data/z", 512, DT_REG, zone_id="corp")
        result = self.dc.get_full("/zoned")
        assert result is not None
        assert result["zone_id"] == "corp"


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_fast extension not available")
class TestRustDCacheEvict(unittest.TestCase):
    def setUp(self) -> None:
        self.dc = RustDCache()
        for i in range(5):
            self.dc.put(f"/docs/file{i}.md", "local", f"/data/file{i}.md", i * 100, DT_REG)
        self.dc.put("/src/main.rs", "local", "/data/main.rs", 2048, DT_REG)

    def test_evict_existing(self) -> None:
        assert self.dc.evict("/docs/file0.md") is True
        assert len(self.dc) == 5

    def test_evict_nonexistent(self) -> None:
        assert self.dc.evict("/nonexistent") is False
        assert len(self.dc) == 6

    def test_evict_prefix(self) -> None:
        count = self.dc.evict_prefix("/docs/")
        assert count == 5
        assert len(self.dc) == 1
        assert self.dc.contains("/src/main.rs")

    def test_evict_prefix_empty(self) -> None:
        count = self.dc.evict_prefix("/nonexistent/")
        assert count == 0
        assert len(self.dc) == 6


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_fast extension not available")
class TestRustDCacheContains(unittest.TestCase):
    def test_contains(self) -> None:
        dc = RustDCache()
        dc.put("/a", "local", "/a", 0, DT_REG)
        assert dc.contains("/a") is True
        assert dc.contains("/b") is False


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_fast extension not available")
class TestRustDCacheStats(unittest.TestCase):
    def test_stats_empty(self) -> None:
        dc = RustDCache()
        stats = dc.stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["size"] == 0
        assert stats["hit_rate"] == 0.0

    def test_stats_with_activity(self) -> None:
        dc = RustDCache()
        dc.put("/a", "local", "/a", 0, DT_REG)
        dc.get("/a")  # hit
        dc.get("/a")  # hit
        dc.get("/miss")  # miss

        stats = dc.stats()
        assert stats["hits"] == 2
        assert stats["misses"] == 1
        assert stats["size"] == 1
        assert abs(stats["hit_rate"] - 2 / 3) < 0.01


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_fast extension not available")
class TestRustDCacheClear(unittest.TestCase):
    def test_clear(self) -> None:
        dc = RustDCache()
        dc.put("/a", "local", "/a", 0, DT_REG)
        dc.put("/b", "local", "/b", 0, DT_REG)
        dc.get("/a")  # hit counter
        assert len(dc) == 2

        dc.clear()
        assert len(dc) == 0
        stats = dc.stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 0


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_fast extension not available")
class TestRustDCacheRepr(unittest.TestCase):
    def test_repr(self) -> None:
        dc = RustDCache()
        dc.put("/a", "local", "/a", 0, DT_REG)
        r = repr(dc)
        assert "RustDCache" in r
        assert "size=1" in r


@unittest.skipUnless(RUST_AVAILABLE, "Rust nexus_fast extension not available")
class TestRustDCacheMetastoreIntegration(unittest.TestCase):
    """Verify MetastoreABC dual-write keeps Python dict and RustDCache in sync."""

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

            def _put_raw(self, metadata, *, consistency="sc"):
                self._store[metadata.path] = metadata
                return None

            def _delete_raw(self, path, *, consistency="sc"):
                return self._store.pop(path, None)

            def _exists_raw(self, path):
                return path in self._store

            def _list_raw(self, prefix="", recursive=True, **kwargs):
                return [m for p, m in self._store.items() if p.startswith(prefix)]

            def close(self):
                pass

        return InMemoryStore()

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

        # First get — dcache miss → populates both caches
        result = store.get("/test/file.txt")
        assert result is not None
        assert store._rust_dcache.contains("/test/file.txt")

        rust_entry = store._rust_dcache.get_full("/test/file.txt")
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

        assert store._rust_dcache.contains("/new/file.txt")
        rust_entry = store._rust_dcache.get_full("/new/file.txt")
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
        assert store._rust_dcache.contains("/del/me.txt")

        store.delete("/del/me.txt")
        assert not store._rust_dcache.contains("/del/me.txt")

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
        assert not store._rust_dcache.contains("/mount/file0")
        assert store._rust_dcache.contains("/other/keep")

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
            assert store._rust_dcache.contains(f"/list/file{i}")

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

        assert len(store._rust_dcache) == 5
        for i in range(5):
            assert store._rust_dcache.contains(f"/batch/{i}")

    def test_cache_stats_includes_rust(self) -> None:
        store = self._make_store()

        stats = store.cache_stats
        assert "rust" in stats
        assert "hits" in stats["rust"]
        assert "misses" in stats["rust"]


if __name__ == "__main__":
    unittest.main()
