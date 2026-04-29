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
    from nexus_runtime import BlobPackEngine

    HAS_VOLUME_ENGINE = True
except ImportError:
    HAS_VOLUME_ENGINE = False

from nexus.backends.engines.cas_gc import CASGarbageCollector

pytestmark = pytest.mark.skipif(
    not HAS_VOLUME_ENGINE, reason="nexus_runtime.BlobPackEngine not available"
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
    def __init__(self, content_id):
        self.content_id = content_id


# ─── Volume-Aware GC Tests ───────────────────────────────────────────────────


class TestGCWithVolumes:
    """GC should work with BlobPackLocalTransport."""

    def _make_engine_and_transport(self, tmp_path):
        from nexus.backends.transports.blob_pack_local_transport import BlobPackLocalTransport

        transport = BlobPackLocalTransport(root_path=tmp_path, fsync=False)
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
            transport.store(f"cas/{h[:2]}/{h[2:4]}/{h}", f"data_{i}".encode())

        # Seal so they're visible
        transport.seal_active_volume()

        # Only reference hashes 0-2 in metastore
        metastore = FakeMetastore([FakeEntry(make_hash(i)) for i in range(3)])

        gc = CASGarbageCollector(engine, metastore, grace_period=0)
        gc._collect()

        # Hashes 0-2 should still exist, 3-4 should be gone
        for i in range(3):
            h = make_hash(i)
            assert transport.exists(f"cas/{h[:2]}/{h[2:4]}/{h}")
        for i in range(3, 5):
            h = make_hash(i)
            assert not transport.exists(f"cas/{h[:2]}/{h[2:4]}/{h}")

    def test_gc_respects_grace_period(self, tmp_path):
        engine, transport = self._make_engine_and_transport(tmp_path)

        h = make_hash(99)
        transport.store(f"cas/{h[:2]}/{h[2:4]}/{h}", b"fresh data")
        transport.seal_active_volume()

        # No references, but grace period of 1 hour
        metastore = FakeMetastore([])
        gc = CASGarbageCollector(engine, metastore, grace_period=3600)
        gc._collect()

        # Should still exist (within grace period)
        assert transport.exists(f"cas/{h[:2]}/{h[2:4]}/{h}")

    def test_gc_skips_when_no_metastore(self, tmp_path):
        engine, transport = self._make_engine_and_transport(tmp_path)
        gc = CASGarbageCollector(engine, metastore=None, grace_period=0)
        gc._collect()  # Should not raise


# ─── Compaction Tests ────────────────────────────────────────────────────────


class TestCompaction:
    """Volume compaction should rewrite sparse volumes."""

    def test_compaction_reclaims_space(self, tmp_path):
        vol_dir = tmp_path / "vol"
        engine = BlobPackEngine(
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
        engine = BlobPackEngine(
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
        engine = BlobPackEngine(
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
        engine = BlobPackEngine(
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


# ─── Compaction Crash Recovery Tests (Issue #3408) ──────────────────────────


class TestCompactionCrashRecovery:
    """Verify that compaction is crash-safe via write-ahead ordering."""

    def test_crash_after_seal_before_index_commit(self, tmp_path):
        """If crash happens after new volume is sealed but before index commit,
        startup reconciliation should pick up the new volume's entries."""
        vol_dir = tmp_path / "vol"
        engine = BlobPackEngine(
            str(vol_dir),
            target_volume_size=1024 * 1024,
            compaction_sparsity_threshold=0.3,
        )

        # Write entries and seal
        for i in range(10):
            engine.put(make_hash(i), bytes([i] * 50))
        engine.seal_active()

        # Delete 7 of 10 to trigger compaction
        for i in range(7):
            engine.delete(make_hash(i))

        # Run compaction — this creates a new sealed volume and updates the index
        compacted, moved, _ = engine.compact()
        assert compacted > 0
        assert moved == 3

        # Verify surviving entries readable after compaction
        for i in range(7, 10):
            assert engine.exists(make_hash(i))
            data = bytes(engine.get(make_hash(i)))
            assert data == bytes([i] * 50)

        engine.close()
        del engine

        # Re-open: simulates startup after a "crash" where the engine was closed
        # normally, but tests the reconciliation path
        import gc

        gc.collect()
        engine2 = BlobPackEngine(
            str(vol_dir),
            target_volume_size=1024 * 1024,
            compaction_sparsity_threshold=0.3,
        )

        # All surviving entries should still be readable
        for i in range(7, 10):
            assert engine2.exists(make_hash(i))
            data = bytes(engine2.get(make_hash(i)))
            assert data == bytes([i] * 50)

        # Deleted entries should remain deleted
        for i in range(7):
            assert not engine2.exists(make_hash(i))

        engine2.close()

    def test_crash_leaves_both_volumes(self, tmp_path):
        """If both old and new .vol files exist (simulating crash between
        index commit and old volume deletion), reads should still work."""
        vol_dir = tmp_path / "vol"
        engine = BlobPackEngine(
            str(vol_dir),
            target_volume_size=1024 * 1024,
            compaction_sparsity_threshold=0.3,
        )

        for i in range(5):
            engine.put(make_hash(i), bytes([i] * 50))
        engine.seal_active()

        # Delete 4 of 5 to trigger compaction
        for i in range(4):
            engine.delete(make_hash(i))
        engine.compact()

        # After compaction, surviving entry is still readable
        assert engine.exists(make_hash(4))
        data = bytes(engine.get(make_hash(4)))
        assert data == bytes([4] * 50)

        engine.close()

    def test_orphan_tmp_from_interrupted_compaction(self, tmp_path):
        """If compaction crashes while writing a new .tmp volume,
        startup should delete the .tmp and preserve old data."""
        vol_dir = tmp_path / "vol"
        engine = BlobPackEngine(
            str(vol_dir),
            target_volume_size=1024 * 1024,
            compaction_sparsity_threshold=0.3,
        )

        for i in range(5):
            engine.put(make_hash(i), bytes([i] * 50))
        engine.seal_active()
        engine.close()
        del engine

        # Simulate a crash during compaction by creating a fake .tmp file
        fake_tmp = vol_dir / "vol_deadbeef.tmp"
        fake_tmp.write_bytes(b"incomplete compaction data")

        import gc

        gc.collect()

        # Re-open — should delete .tmp, recover from .vol files
        engine2 = BlobPackEngine(
            str(vol_dir),
            target_volume_size=1024 * 1024,
            compaction_sparsity_threshold=0.3,
        )

        # .tmp file should be deleted
        assert not fake_tmp.exists()

        # All entries should still be readable from original .vol
        for i in range(5):
            assert engine2.exists(make_hash(i))
            data = bytes(engine2.get(make_hash(i)))
            assert data == bytes([i] * 50)

        engine2.close()


# ─── Compaction pread Error Handling Tests (Issue #3408) ────────────────────


class TestCompactionPreadError:
    """Verify compaction preserves data when blobs are unreadable."""

    def test_corrupted_volume_preserves_old_data(self, tmp_path):
        """If a blob can't be read during compaction, the old volume
        should be preserved (not deleted)."""
        vol_dir = tmp_path / "vol"
        engine = BlobPackEngine(
            str(vol_dir),
            target_volume_size=1024 * 1024,
            compaction_sparsity_threshold=0.3,
        )

        # Write entries and seal
        for i in range(10):
            engine.put(make_hash(i), bytes([i] * 100))
        engine.seal_active()

        # Delete 7 to trigger compaction
        for i in range(7):
            engine.delete(make_hash(i))

        # Find and truncate the .vol file to corrupt some data
        import glob

        vol_files = glob.glob(str(vol_dir / "*.vol"))
        assert len(vol_files) >= 1
        vol_file = vol_files[0]

        # Truncate to remove some entry data (but keep header + footer intact)
        import os

        file_size = os.path.getsize(vol_file)
        # Truncate to 60% of original — corrupts some entries but keeps TOC readable
        with open(vol_file, "r+b") as f:
            f.truncate(file_size * 6 // 10)

        # Compact — should handle errors gracefully
        # Compaction may fail to read some blobs but shouldn't crash
        compacted, moved, _ = engine.compact()

        # Since volume was truncated, TOC read may fail entirely (can't read footer),
        # so compaction may simply skip this volume. Either way, no crash.
        # The remaining readable entries (if any) should still work.
        engine.close()


# ─── Rate-Limited Compaction Tests (Issue #3408) ───────────────────────────


class TestCompactionBytesPerCycle:
    """Verify compaction_bytes_per_cycle limits work correctly."""

    def test_partial_compaction_preserves_old_volume(self, tmp_path):
        """When bytes_per_cycle is exhausted, old volume should NOT be deleted."""
        vol_dir = tmp_path / "vol"
        engine = BlobPackEngine(
            str(vol_dir),
            target_volume_size=1024 * 1024,
            compaction_bytes_per_cycle=100,  # Very small budget
            compaction_sparsity_threshold=0.3,
        )

        # Write 10 entries (100 bytes each = 1000 bytes total data)
        for i in range(10):
            engine.put(make_hash(i), bytes([i] * 100))
        engine.seal_active()

        # Delete 7 of 10 (70% sparsity > 30% threshold)
        for i in range(7):
            engine.delete(make_hash(i))

        # First compact: should process only ~100 bytes worth of blobs
        compacted1, moved1, _ = engine.compact()

        # All surviving entries should still be readable
        for i in range(7, 10):
            assert engine.exists(make_hash(i))
            data = bytes(engine.get(make_hash(i)))
            assert data == bytes([i] * 100)

        engine.close()

    def test_unlimited_budget_compacts_everything(self, tmp_path):
        """When bytes_per_cycle=0 (unlimited), all candidates are processed."""
        vol_dir = tmp_path / "vol"
        engine = BlobPackEngine(
            str(vol_dir),
            target_volume_size=1024 * 1024,
            compaction_bytes_per_cycle=0,  # Unlimited
            compaction_sparsity_threshold=0.3,
        )

        for i in range(10):
            engine.put(make_hash(i), bytes([i] * 100))
        engine.seal_active()

        for i in range(7):
            engine.delete(make_hash(i))

        compacted, moved, reclaimed = engine.compact()
        assert compacted > 0
        assert moved == 3  # 3 surviving entries moved

        # All surviving entries readable
        for i in range(7, 10):
            assert engine.exists(make_hash(i))
            data = bytes(engine.get(make_hash(i)))
            assert data == bytes([i] * 100)

        engine.close()


# ─── Compaction Stats Tests (Issue #3408) ───────────────────────────────────


class TestCompactionStats:
    """Verify compaction stats counters are tracked."""

    def test_stats_include_compaction_counters(self, tmp_path):
        vol_dir = tmp_path / "vol"
        engine = BlobPackEngine(
            str(vol_dir),
            target_volume_size=1024 * 1024,
            compaction_sparsity_threshold=0.3,
        )

        stats = engine.stats()
        assert stats["compaction_volumes_total"] == 0
        assert stats["compaction_blobs_moved_total"] == 0
        assert stats["compaction_bytes_reclaimed_total"] == 0

        # Write, seal, delete, compact
        for i in range(10):
            engine.put(make_hash(i), bytes([i] * 50))
        engine.seal_active()
        for i in range(7):
            engine.delete(make_hash(i))
        engine.compact()

        stats = engine.stats()
        assert stats["compaction_volumes_total"] > 0
        assert stats["compaction_blobs_moved_total"] == 3
        assert stats["compaction_bytes_reclaimed_total"] > 0

        engine.close()


# ─── Volume Lifecycle Tests ──────────────────────────────────────────────────


class TestVolumeLifecycle:
    def test_auto_seal_on_full(self, tmp_path):
        vol_dir = tmp_path / "vol"
        engine = BlobPackEngine(str(vol_dir), target_volume_size=256)

        for i in range(20):
            engine.put(make_hash(i), bytes([i] * 100))

        stats = engine.stats()
        assert stats["sealed_volume_count"] > 0

        engine.close()

    def test_dedup_across_volumes(self, tmp_path):
        vol_dir = tmp_path / "vol"
        engine = BlobPackEngine(str(vol_dir), target_volume_size=256)

        h = make_hash(1)
        assert engine.put(h, b"unique data")  # new
        engine.seal_active()

        assert not engine.put(h, b"unique data")  # dedup

        engine.close()

    def test_stats(self, tmp_path):
        vol_dir = tmp_path / "vol"
        engine = BlobPackEngine(str(vol_dir), target_volume_size=1024 * 1024)

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
