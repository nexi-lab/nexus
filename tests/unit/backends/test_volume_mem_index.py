"""Tests for in-memory volume index — O(1) content lookup.

Tests:
  - read_content fast path (HashMap lookup + pread from cached FD)
  - Consistency: mem_index mirrors writes and deletes
  - Startup index load from redb
  - Memory reporting
  - Sealed volume FD caching
  - Compaction updates mem_index

Issue #3404: in-memory volume index.
"""

from __future__ import annotations

import pytest

try:
    from nexus_fast import VolumeEngine

    HAS_VOLUME_ENGINE = True
except ImportError:
    HAS_VOLUME_ENGINE = False

pytestmark = pytest.mark.skipif(
    not HAS_VOLUME_ENGINE, reason="nexus_fast.VolumeEngine not available"
)


def make_hash(seed: int) -> str:
    return f"{seed:064x}"


class TestMemIndexReadContent:
    """Test read_content fast path (lookup + pread in single Rust call)."""

    def test_read_content_after_put(self, tmp_path):
        """read_content works immediately after put (active volume fallback)."""
        engine = VolumeEngine(str(tmp_path / "vol"), target_volume_size=1024 * 1024)
        h = make_hash(1)
        engine.put(h, b"hello from active volume")
        data = engine.read_content(h)
        assert bytes(data) == b"hello from active volume"

    def test_read_content_after_seal(self, tmp_path):
        """read_content uses cached FD after seal (pread fast path)."""
        engine = VolumeEngine(str(tmp_path / "vol"), target_volume_size=1024 * 1024)
        h = make_hash(1)
        engine.put(h, b"hello from sealed volume")
        engine.seal_active()

        data = engine.read_content(h)
        assert bytes(data) == b"hello from sealed volume"

        # Verify cached FD is registered
        stats = engine.stats()
        assert stats["mem_index_volumes"] >= 1

    def test_read_content_not_found(self, tmp_path):
        """read_content returns None for missing hash."""
        engine = VolumeEngine(str(tmp_path / "vol"))
        assert engine.read_content(make_hash(999)) is None

    def test_read_content_multiple_volumes(self, tmp_path):
        """read_content works across multiple sealed volumes."""
        engine = VolumeEngine(str(tmp_path / "vol"), target_volume_size=256)
        data_map = {}

        for i in range(20):
            h = make_hash(i)
            data = f"content_{i}".encode()
            engine.put(h, data)
            data_map[h] = data

        engine.seal_active()

        # Read all back
        for h, expected in data_map.items():
            result = engine.read_content(h)
            assert result is not None, f"Missing hash {h}"
            assert bytes(result) == expected

    def test_read_content_large_blobs(self, tmp_path):
        """read_content handles blobs of various sizes."""
        engine = VolumeEngine(str(tmp_path / "vol"), target_volume_size=1024 * 1024)

        sizes = [0, 1, 100, 4096, 65536]
        for i, size in enumerate(sizes):
            h = make_hash(i)
            data = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
            engine.put(h, data)

        engine.seal_active()

        for i, size in enumerate(sizes):
            h = make_hash(i)
            result = engine.read_content(h)
            assert result is not None
            assert len(result) == size


class TestMemIndexConsistency:
    """Test that mem_index stays consistent with writes/deletes."""

    def test_exists_uses_mem_index(self, tmp_path):
        """exists() returns O(1) via mem_index."""
        engine = VolumeEngine(str(tmp_path / "vol"))
        h = make_hash(1)

        assert not engine.exists(h)
        engine.put(h, b"data")
        assert engine.exists(h)
        engine.delete(h)
        assert not engine.exists(h)

    def test_get_size_uses_mem_index(self, tmp_path):
        """get_size() returns O(1) via mem_index."""
        engine = VolumeEngine(str(tmp_path / "vol"))
        h = make_hash(1)
        data = b"exactly 17 bytes!"

        assert engine.get_size(h) is None
        engine.put(h, data)
        assert engine.get_size(h) == len(data)

    def test_dedup_via_mem_index(self, tmp_path):
        """put() dedup check uses mem_index (skips redb)."""
        engine = VolumeEngine(str(tmp_path / "vol"))
        h = make_hash(1)

        assert engine.put(h, b"first") is True  # new
        assert engine.put(h, b"first") is False  # dedup via mem_index

    def test_delete_removes_from_mem_index(self, tmp_path):
        """delete() removes from mem_index so subsequent reads return None."""
        engine = VolumeEngine(str(tmp_path / "vol"))
        h = make_hash(1)

        engine.put(h, b"to delete")
        engine.seal_active()
        assert engine.read_content(h) is not None

        engine.delete(h)
        assert engine.read_content(h) is None
        assert not engine.exists(h)

    def test_batch_get_uses_mem_index(self, tmp_path):
        """batch_get uses mem_index for O(1) lookups."""
        engine = VolumeEngine(str(tmp_path / "vol"), target_volume_size=1024 * 1024)
        hashes = [make_hash(i) for i in range(10)]

        for h in hashes:
            engine.put(h, f"data_{h[:8]}".encode())
        engine.seal_active()

        result = engine.batch_get(hashes)
        assert len(result) == 10
        for h in hashes:
            assert h in result


class TestMemIndexStartupLoad:
    """Test that mem_index is populated from redb on startup."""

    def test_startup_loads_index(self, tmp_path):
        """New engine instance loads existing index into mem_index."""
        vol_dir = str(tmp_path / "vol")

        # Create and populate
        engine1 = VolumeEngine(vol_dir, target_volume_size=1024 * 1024)
        hashes = [make_hash(i) for i in range(50)]
        for h in hashes:
            engine1.put(h, f"startup_{h[:8]}".encode())
        engine1.seal_active()
        engine1.close()
        del engine1  # Release redb lock

        # Re-open — should load index from redb
        engine2 = VolumeEngine(vol_dir, target_volume_size=1024 * 1024)
        stats = engine2.stats()
        assert stats["mem_index_entries"] == 50
        assert stats["mem_index_volumes"] >= 1

        # All hashes should be readable via fast path
        for h in hashes:
            assert engine2.exists(h)
            data = engine2.read_content(h)
            assert data is not None
            assert bytes(data) == f"startup_{h[:8]}".encode()

    def test_startup_opens_volume_fds(self, tmp_path):
        """Startup caches FDs for sealed volumes."""
        vol_dir = str(tmp_path / "vol")

        engine1 = VolumeEngine(vol_dir, target_volume_size=256)
        for i in range(20):
            engine1.put(make_hash(i), b"x" * 100)
        engine1.seal_active()
        sealed_count = engine1.stats()["sealed_volume_count"]
        engine1.close()
        del engine1  # Release redb lock

        engine2 = VolumeEngine(vol_dir, target_volume_size=256)
        assert engine2.stats()["mem_index_volumes"] == sealed_count


class TestMemIndexMemory:
    """Test memory reporting."""

    def test_memory_bytes_grows(self, tmp_path):
        """index_memory_bytes grows with entries."""
        engine = VolumeEngine(str(tmp_path / "vol"))
        base = engine.index_memory_bytes()

        for i in range(1000):
            engine.put(make_hash(i), b"x")

        loaded = engine.index_memory_bytes()
        assert loaded > base

        per_entry = loaded / 1000
        # Should be < 100 bytes per entry (32 key + 16 value + overhead)
        assert per_entry < 100, f"per_entry={per_entry} too high"

    def test_stats_include_mem_index(self, tmp_path):
        """stats() includes mem_index info."""
        engine = VolumeEngine(str(tmp_path / "vol"))
        engine.put(make_hash(1), b"data")
        stats = engine.stats()

        assert "mem_index_entries" in stats
        assert "mem_index_bytes" in stats
        assert "mem_index_volumes" in stats
        assert stats["mem_index_entries"] == 1


class TestMemIndexCompaction:
    """Test that compaction updates mem_index entries and FDs."""

    def test_compaction_updates_mem_index(self, tmp_path):
        """After compaction, entries point to new volumes and are still readable."""
        engine = VolumeEngine(
            str(tmp_path / "vol"),
            target_volume_size=512,
            compaction_rate_limit=0,
            compaction_sparsity_threshold=0.3,
        )

        # Write 10 entries, seal
        for i in range(10):
            engine.put(make_hash(i), bytes([i] * 50))
        engine.seal_active()

        # Delete 7 (70% sparsity)
        for i in range(7):
            engine.delete(make_hash(i))

        # Compact
        compacted, moved, _ = engine.compact()
        assert compacted > 0

        # Remaining entries should still be readable via mem_index
        for i in range(7, 10):
            h = make_hash(i)
            assert engine.exists(h)
            data = engine.read_content(h)
            assert data is not None
            assert bytes(data) == bytes([i] * 50)

        # Deleted entries should still be gone
        for i in range(7):
            assert not engine.exists(make_hash(i))
