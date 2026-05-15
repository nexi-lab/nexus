"""Volume engine crash recovery tests.

Tests the TOC-at-end crash recovery pattern:
  - Active volumes (.tmp) are deleted on startup
  - Sealed volumes (.vol) can rebuild index from their TOC
  - Stale index entries (pointing to deleted volumes) are cleaned up
  - Truncated/corrupted volumes are handled gracefully

Issue #3403: CAS volume packing — crash safety.
"""

from __future__ import annotations

import gc

import pytest

try:
    from nexus_runtime import BlobPackEngine

    HAS_VOLUME_ENGINE = True
except ImportError:
    HAS_VOLUME_ENGINE = False

pytestmark = pytest.mark.skipif(
    not HAS_VOLUME_ENGINE, reason="nexus_runtime.BlobPackEngine not available"
)


def make_hash(seed: int) -> str:
    """Generate a deterministic 64-char hex hash."""
    return f"{seed:064x}"


class TestCrashRecoveryDeletesTmpFiles:
    """Active volumes (.tmp files) should be deleted on startup."""

    def test_orphan_tmp_deleted(self, tmp_path):
        vol_dir = tmp_path / "volumes"
        vol_dir.mkdir()

        # Create a fake .tmp file (simulating crash during write)
        tmp_file = vol_dir / "vol_00000001.tmp"
        tmp_file.write_bytes(b"incomplete volume data")
        assert tmp_file.exists()

        # Creating engine should delete .tmp
        engine = BlobPackEngine(str(vol_dir))
        assert not tmp_file.exists()
        engine.close()

    def test_multiple_tmp_files_deleted(self, tmp_path):
        vol_dir = tmp_path / "volumes"
        vol_dir.mkdir()

        tmps = []
        for i in range(5):
            f = vol_dir / f"vol_{i:08x}.tmp"
            f.write_bytes(b"incomplete")
            tmps.append(f)

        engine = BlobPackEngine(str(vol_dir))
        for f in tmps:
            assert not f.exists()
        engine.close()


class TestCrashRecoveryRebuildsFromVol:
    """Sealed volumes can rebuild the index from their TOC."""

    def test_rebuild_index_from_sealed_volume(self, tmp_path):
        vol_dir = tmp_path / "volumes"

        # Phase 1: Create engine, write data, seal, close
        engine = BlobPackEngine(str(vol_dir), target_volume_size=1024 * 1024)
        h = make_hash(42)
        engine.put(h, b"important data")
        engine.seal_active()
        engine.close()
        del engine
        gc.collect()

        # Verify .vol file exists
        vol_files = list(vol_dir.glob("*.vol"))
        assert len(vol_files) >= 1

        # Phase 2: Delete the index, recreate engine → should rebuild from .vol TOC
        index_path = vol_dir / "volume_index.redb"
        assert index_path.exists()
        index_path.unlink()

        engine2 = BlobPackEngine(str(vol_dir), target_volume_size=1024 * 1024)
        assert engine2.exists(h)
        data = engine2.get(h)
        assert bytes(data) == b"important data"
        engine2.close()

    def test_rebuild_multiple_volumes(self, tmp_path):
        vol_dir = tmp_path / "volumes"

        # Write enough data to fill multiple volumes (small target)
        engine = BlobPackEngine(str(vol_dir), target_volume_size=256)
        hashes = []
        for i in range(20):
            h = make_hash(i)
            engine.put(h, bytes([i] * 100))
            hashes.append(h)
        engine.seal_active()
        engine.close()
        del engine
        gc.collect()

        # Delete index
        (vol_dir / "volume_index.redb").unlink()

        # Rebuild
        engine2 = BlobPackEngine(str(vol_dir), target_volume_size=256)
        for h in hashes:
            assert engine2.exists(h), f"Hash {h[:16]}... not found after rebuild"
        engine2.close()


class TestStaleIndexEntries:
    """Index entries pointing to deleted volumes should be cleaned up."""

    def test_stale_entries_removed(self, tmp_path):
        vol_dir = tmp_path / "volumes"

        # Create data and seal
        engine = BlobPackEngine(str(vol_dir), target_volume_size=1024 * 1024)
        h = make_hash(1)
        engine.put(h, b"will be orphaned")
        engine.seal_active()
        assert engine.exists(h)
        engine.close()
        del engine
        gc.collect()

        # Delete the volume file but keep the index
        for f in vol_dir.glob("*.vol"):
            f.unlink()

        # Reopen — engine should detect stale entries and clean them
        engine2 = BlobPackEngine(str(vol_dir), target_volume_size=1024 * 1024)
        assert not engine2.exists(h), "Stale entry should be removed"
        engine2.close()


class TestCorruptedVolumes:
    """Corrupted volume files should be skipped gracefully."""

    def test_truncated_volume_skipped(self, tmp_path):
        vol_dir = tmp_path / "volumes"
        vol_dir.mkdir()

        # Create a truncated .vol file (too small for header + footer)
        corrupt = vol_dir / "vol_00000001.vol"
        corrupt.write_bytes(b"too short")

        # Engine should log warning and skip
        engine = BlobPackEngine(str(vol_dir))
        assert engine.len() == 0
        engine.close()

    def test_bad_magic_skipped(self, tmp_path):
        vol_dir = tmp_path / "volumes"
        vol_dir.mkdir()

        # Create a .vol file with wrong magic
        corrupt = vol_dir / "vol_00000001.vol"
        data = bytearray(128)
        data[0:4] = b"BAAD"  # Wrong magic
        corrupt.write_bytes(bytes(data))

        engine = BlobPackEngine(str(vol_dir))
        assert engine.len() == 0
        engine.close()

    def test_bad_footer_checksum_skipped(self, tmp_path):
        vol_dir = tmp_path / "volumes"

        # First create a valid volume
        engine = BlobPackEngine(str(vol_dir), target_volume_size=1024 * 1024)
        engine.put(make_hash(1), b"valid data")
        engine.seal_active()
        engine.close()

        # Corrupt the footer checksum of the .vol file
        vol_files = list(vol_dir.glob("*.vol"))
        assert len(vol_files) == 1
        vol_file = vol_files[0]
        data = bytearray(vol_file.read_bytes())
        # Footer is last 24 bytes, checksum is last 4 bytes of footer
        data[-4:] = b"\xff\xff\xff\xff"
        vol_file.write_bytes(bytes(data))

        # Delete index and recreate — should skip corrupted volume
        (vol_dir / "volume_index.redb").unlink()
        engine2 = BlobPackEngine(str(vol_dir), target_volume_size=1024 * 1024)
        assert engine2.len() == 0, "Corrupted volume should be skipped"
        engine2.close()


class TestGracefulRecovery:
    """Engine recovers gracefully from various failure states."""

    def test_empty_directory(self, tmp_path):
        vol_dir = tmp_path / "volumes"
        engine = BlobPackEngine(str(vol_dir))
        assert engine.len() == 0
        assert engine.total_bytes() == 0
        engine.close()

    def test_recovery_preserves_data_integrity(self, tmp_path):
        """End-to-end: write → crash → recover → verify all data."""
        vol_dir = tmp_path / "volumes"

        # Write data across multiple volumes
        engine = BlobPackEngine(str(vol_dir), target_volume_size=512)
        expected = {}
        for i in range(30):
            h = make_hash(i)
            data = f"content_{i}_{'x' * 50}".encode()
            engine.put(h, data)
            expected[h] = data

        engine.seal_active()
        engine.close()
        del engine
        gc.collect()

        # Simulate crash: delete index, leave .vol files
        (vol_dir / "volume_index.redb").unlink()

        # Recover
        engine2 = BlobPackEngine(str(vol_dir), target_volume_size=512)

        # Verify all data
        for h, expected_data in expected.items():
            assert engine2.exists(h), f"Hash {h[:16]}... missing after recovery"
            actual = bytes(engine2.get(h))
            assert actual == expected_data, f"Data mismatch for {h[:16]}..."

        engine2.close()

    def test_concurrent_open_same_dir(self, tmp_path):
        """Opening the same volume directory twice should not corrupt data."""
        vol_dir = tmp_path / "volumes"

        engine1 = BlobPackEngine(str(vol_dir), target_volume_size=1024 * 1024)
        engine1.put(make_hash(1), b"from engine 1")
        engine1.seal_active()
        engine1.close()
        del engine1
        gc.collect()

        engine2 = BlobPackEngine(str(vol_dir), target_volume_size=1024 * 1024)
        assert engine2.exists(make_hash(1))
        assert bytes(engine2.get(make_hash(1))) == b"from engine 1"
        engine2.close()

    def test_deleted_blob_not_resurrected_on_restart(self, tmp_path):
        """Delete before seal must not be resurrected by crash recovery.

        Regression test: delete() removes from index, but if the blob's TOC
        entry survives into the sealed volume, recovery would re-insert it.
        The fix filters deleted entries from the TOC at seal time.
        """
        vol_dir = tmp_path / "volumes"

        engine = BlobPackEngine(str(vol_dir), target_volume_size=1024 * 1024)
        engine.put(make_hash(1), b"keep me")
        engine.put(make_hash(2), b"delete me")

        # Delete before sealing
        engine.delete(make_hash(2))
        assert not engine.exists(make_hash(2))

        # Close (which seals the active volume)
        engine.close()
        del engine
        gc.collect()

        # Reopen — deleted blob must NOT reappear
        engine2 = BlobPackEngine(str(vol_dir), target_volume_size=1024 * 1024)
        assert engine2.exists(make_hash(1)), "Kept blob should survive restart"
        assert not engine2.exists(make_hash(2)), "Deleted blob must not be resurrected"
        engine2.close()
