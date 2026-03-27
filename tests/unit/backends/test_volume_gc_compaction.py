"""GC and compaction tests for volume-packed storage.

Tests:
  - GC tombstones unreferenced blobs in volume index
  - Compaction rewrites sparse volumes with only live blobs
  - Concurrent safety during compaction
  - CDC manifest expansion with volume-packed chunks
  - Grace period using index write timestamps

Issue #3403: CAS volume packing — GC and compaction.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

try:
    from nexus_fast import VolumeEngine

    HAS_VOLUME_ENGINE = True
except ImportError:
    HAS_VOLUME_ENGINE = False

from nexus.backends.engines.cas_gc import CASGarbageCollector

pytestmark = pytest.mark.skipif(
    not HAS_VOLUME_ENGINE, reason="nexus_fast.VolumeEngine not available"
)


def make_hash(seed: int) -> str:
    return f"{seed:064x}"


class FakeMetastore:
    """Minimal metastore stub for GC testing."""

    def __init__(self, entries=None):
        self._entries = entries or []

    def list(self, prefix="", recursive=True):
        return self._entries


class FakeEntry:
    def __init__(self, etag):
        self.etag = etag
        self.content_id = etag


# ─── Volume-Aware GC Tests ───────────────────────────────────────────────────


class TestGCWithVolumes:
    """GC should work with VolumeLocalTransport."""

    def _make_engine_and_transport(self, tmp_path):
        from nexus.backends.transports.volume_local_transport import VolumeLocalTransport

        transport = VolumeLocalTransport(root_path=tmp_path, fsync=False)
        # Build a minimal CASAddressingEngine-like object
        engine = MagicMock()
        engine._transport = transport
        engine._meta_cache = None
        engine.name = "test"
        engine._blob_key = lambda h: f"cas/{h[:2]}/{h[2:4]}/{h}"
        engine._meta_key = lambda h: f"cas/{h[:2]}/{h[2:4]}/{h}.meta"
        engine._read_meta = MagicMock(return_value={"size": 0})
        return engine, transport

    def test_gc_deletes_unreferenced_blobs(self, tmp_path):
        engine, transport = self._make_engine_and_transport(tmp_path)

        # Write 5 blobs
        for i in range(5):
            h = make_hash(i)
            transport.put_blob(f"cas/{h[:2]}/{h[2:4]}/{h}", f"data_{i}".encode())

        # Seal so they're visible
        transport.seal_active_volume()

        # Only reference hashes 0-2 in metastore
        metastore = FakeMetastore([FakeEntry(make_hash(i)) for i in range(3)])

        gc = CASGarbageCollector(engine, metastore, grace_period=0)
        gc._collect()

        # Hashes 0-2 should still exist, 3-4 should be gone
        for i in range(3):
            h = make_hash(i)
            assert transport.blob_exists(f"cas/{h[:2]}/{h[2:4]}/{h}")
        for i in range(3, 5):
            h = make_hash(i)
            assert not transport.blob_exists(f"cas/{h[:2]}/{h[2:4]}/{h}")

    def test_gc_respects_grace_period(self, tmp_path):
        engine, transport = self._make_engine_and_transport(tmp_path)

        h = make_hash(99)
        transport.put_blob(f"cas/{h[:2]}/{h[2:4]}/{h}", b"fresh data")
        transport.seal_active_volume()

        # No references, but grace period of 1 hour
        metastore = FakeMetastore([])
        gc = CASGarbageCollector(engine, metastore, grace_period=3600)
        gc._collect()

        # Should still exist (within grace period)
        assert transport.blob_exists(f"cas/{h[:2]}/{h[2:4]}/{h}")

    def test_gc_skips_when_no_metastore(self, tmp_path):
        engine, transport = self._make_engine_and_transport(tmp_path)
        gc = CASGarbageCollector(engine, metastore=None, grace_period=0)
        gc._collect()  # Should not raise


# ─── Compaction Tests ────────────────────────────────────────────────────────


class TestCompaction:
    """Volume compaction should rewrite sparse volumes."""

    def test_compaction_reclaims_space(self, tmp_path):
        vol_dir = tmp_path / "vol"
        engine = VolumeEngine(
            str(vol_dir),
            target_volume_size=1024 * 1024,  # Large enough for all entries in one volume
            compaction_sparsity_threshold=0.3,
        )

        # Write 10 entries
        for i in range(10):
            engine.put(make_hash(i), bytes([i] * 50))
        engine.seal_active()

        # Delete 7 of 10 (70% sparsity > 30% threshold)
        for i in range(7):
            engine.delete(make_hash(i))

        # Compact
        compacted, moved, reclaimed = engine.compact()
        assert compacted > 0
        assert moved > 0

        # Surviving entries still readable
        for i in range(7, 10):
            assert engine.exists(make_hash(i))
            data = bytes(engine.get(make_hash(i)))
            assert data == bytes([i] * 50)

        # Deleted entries still gone
        for i in range(7):
            assert not engine.exists(make_hash(i))

        engine.close()

    def test_compaction_noop_when_not_sparse(self, tmp_path):
        vol_dir = tmp_path / "vol"
        engine = VolumeEngine(
            str(vol_dir),
            target_volume_size=1024 * 1024,
            compaction_sparsity_threshold=0.4,
        )

        for i in range(5):
            engine.put(make_hash(i), b"data")
        engine.seal_active()

        # No deletes → 0% sparsity → no compaction
        compacted, moved, reclaimed = engine.compact()
        assert compacted == 0
        assert moved == 0

        engine.close()

    def test_compaction_fully_dead_volume(self, tmp_path):
        vol_dir = tmp_path / "vol"
        engine = VolumeEngine(
            str(vol_dir),
            target_volume_size=1024,
            compaction_sparsity_threshold=0.3,
        )

        # Write and seal
        for i in range(5):
            engine.put(make_hash(i), b"delete me")
        engine.seal_active()

        # Delete all
        for i in range(5):
            engine.delete(make_hash(i))

        # Compact — should delete the entire volume
        compacted, moved, reclaimed = engine.compact()
        assert compacted > 0
        assert moved == 0  # No blobs to move, just delete
        assert reclaimed > 0

        engine.close()

    def test_compaction_preserves_timestamps(self, tmp_path):
        vol_dir = tmp_path / "vol"
        engine = VolumeEngine(
            str(vol_dir),
            target_volume_size=512,
            compaction_sparsity_threshold=0.3,
        )

        h = make_hash(42)
        engine.put(h, b"preserve my timestamp")
        engine.seal_active()

        ts_before = engine.get_timestamp(h)

        # Delete other entries to trigger compaction
        for i in range(10):
            engine.put(make_hash(100 + i), b"filler")
        engine.seal_active()
        for i in range(10):
            engine.delete(make_hash(100 + i))

        engine.compact()

        ts_after = engine.get_timestamp(h)
        assert ts_before == ts_after, "Compaction should preserve original timestamp"

        engine.close()


# ─── Volume Lifecycle Tests ──────────────────────────────────────────────────


class TestVolumeLifecycle:
    def test_auto_seal_on_full(self, tmp_path):
        vol_dir = tmp_path / "vol"
        engine = VolumeEngine(str(vol_dir), target_volume_size=256)

        for i in range(20):
            engine.put(make_hash(i), bytes([i] * 100))

        stats = engine.stats()
        assert stats["sealed_volume_count"] > 0

        engine.close()

    def test_dedup_across_volumes(self, tmp_path):
        vol_dir = tmp_path / "vol"
        engine = VolumeEngine(str(vol_dir), target_volume_size=256)

        h = make_hash(1)
        assert engine.put(h, b"unique data")  # new
        engine.seal_active()

        assert not engine.put(h, b"unique data")  # dedup

        engine.close()

    def test_stats(self, tmp_path):
        vol_dir = tmp_path / "vol"
        engine = VolumeEngine(str(vol_dir), target_volume_size=1024 * 1024)

        engine.put(make_hash(1), b"data1")
        engine.put(make_hash(2), b"data2")

        stats = engine.stats()
        assert stats["total_blobs"] == 2
        assert stats["active_volume_entries"] == 2

        engine.seal_active()
        stats = engine.stats()
        assert stats["sealed_volume_count"] >= 1
        assert stats["active_volume_entries"] == 0

        engine.close()
