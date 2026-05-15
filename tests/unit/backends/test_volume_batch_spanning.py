"""Multi-volume spanning and reservation lifecycle tests for batch pre-allocation.

Tests that batch pre-allocation correctly spans multiple volumes when data
exceeds target_volume_size, and that reservation lifecycle (create, write,
commit, expire) works correctly with two-phase visibility.

Decisions #10A (multi-volume spanning), #11A (reservation lifecycle).
Issue #3409: Batch pre-allocation for bulk import.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import os

import pytest

nf = pytest.importorskip("nexus_runtime")
VolumeEngine = nf.BlobPackEngine

# 64KB target so spanning happens quickly
TARGET_SIZE = 64 * 1024


def make_data(seed: int, size: int) -> tuple[str, bytes]:
    """Generate deterministic data of a given size, returning (hash_hex, data)."""
    data = bytes([seed & 0xFF] * size)
    hash_hex = hashlib.sha256(data).hexdigest()
    return hash_hex, data


def batch_write(engine, items: list[tuple[str, bytes]]) -> int:
    """Preallocate + parallel write_slot + commit_batch. Returns reservation_id."""
    sizes = [len(d) for _, d in items]
    res_id = engine.preallocate(sizes)
    max_workers = min(len(items), os.cpu_count() or 4)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(engine.write_slot, res_id, i, h, d) for i, (h, d) in enumerate(items)
        ]
        concurrent.futures.wait(futures)
        for f in futures:
            f.result()  # raise if any slot write failed
    engine.commit_batch(res_id)
    return res_id


# ─── Multi-Volume Spanning ─────────────────────────────────────────────────


class TestMultiVolumeSpanning:
    """Batch pre-allocation spanning multiple volumes (Decision #10A)."""

    def test_batch_spans_two_volumes(self, tmp_path):
        """Items totaling >64KB should span at least 2 volumes."""
        engine = VolumeEngine(str(tmp_path / "volumes"), target_volume_size=TARGET_SIZE)

        # Create items totaling ~80KB (> 64KB target)
        items = [make_data(i, 8 * 1024) for i in range(10)]  # 10 x 8KB = 80KB

        batch_write(engine, items)

        # All items should be readable
        for hash_hex, expected in items:
            actual = bytes(engine.get(hash_hex))
            assert actual == expected

        stats = engine.stats()
        assert stats["sealed_volume_count"] >= 2

        engine.close()

    def test_batch_spans_three_volumes(self, tmp_path):
        """Items totaling >128KB should span at least 3 volumes."""
        engine = VolumeEngine(str(tmp_path / "volumes"), target_volume_size=TARGET_SIZE)

        # Create items totaling ~150KB (> 128KB target)
        items = [make_data(i, 10 * 1024) for i in range(15)]  # 15 x 10KB = 150KB

        batch_write(engine, items)

        for hash_hex, expected in items:
            actual = bytes(engine.get(hash_hex))
            assert actual == expected

        stats = engine.stats()
        assert stats["sealed_volume_count"] >= 3

        engine.close()

    def test_batch_spanning_preserves_content(self, tmp_path):
        """Varied-size items preserve exact content across volume boundaries."""
        engine = VolumeEngine(str(tmp_path / "volumes"), target_volume_size=TARGET_SIZE)

        # Varied sizes that will force spanning
        sizes = [1024, 5 * 1024, 10 * 1024, 20 * 1024]
        items = [make_data(i, s) for i, s in enumerate(sizes)]

        batch_write(engine, items)

        for hash_hex, expected in items:
            actual = bytes(engine.get(hash_hex))
            assert actual == expected, (
                f"Content mismatch for hash {hash_hex[:16]}...: "
                f"expected {len(expected)} bytes, got {len(actual)} bytes"
            )

        engine.close()

    def test_batch_spanning_with_varied_sizes(self, tmp_path):
        """Mix of tiny and large items all readable after spanning commit."""
        engine = VolumeEngine(str(tmp_path / "volumes"), target_volume_size=TARGET_SIZE)

        items = []
        # Tiny items (10 bytes each)
        for i in range(20):
            items.append(make_data(i, 10))
        # Large items (30KB each) -- enough to force spanning
        for i in range(100, 105):
            items.append(make_data(i, 30 * 1024))

        batch_write(engine, items)

        for hash_hex, expected in items:
            actual = bytes(engine.get(hash_hex))
            assert actual == expected

        engine.close()

    def test_commit_batch_updates_all_volume_indexes(self, tmp_path):
        """After spanning commit, exists() and get_size() work for all entries."""
        engine = VolumeEngine(str(tmp_path / "volumes"), target_volume_size=TARGET_SIZE)

        items = [make_data(i, 8 * 1024) for i in range(10)]  # 80KB total, spans 2+ volumes

        batch_write(engine, items)

        for hash_hex, data in items:
            assert engine.exists(hash_hex), f"exists() returned False for {hash_hex[:16]}..."
            size = engine.get_size(hash_hex)
            assert size == len(data), (
                f"get_size() returned {size} for {hash_hex[:16]}..., expected {len(data)}"
            )

        engine.close()


# ─── Reservation Lifecycle ─────────────────────────────────────────────────


class TestReservationLifecycle:
    """Reservation create/write/commit/expire lifecycle (Decision #11A)."""

    def test_fresh_reservation_not_expired(self, tmp_path):
        """A freshly created reservation should not be expired."""
        engine = VolumeEngine(str(tmp_path / "volumes"), target_volume_size=TARGET_SIZE)

        items = [make_data(0, 100)]
        sizes = [len(d) for _, d in items]
        res_id = engine.preallocate(sizes)

        # No reservations should be expired yet
        expired = engine.expire_reservations()
        assert expired == 0

        # Write and commit should still work
        hash_hex, data = items[0]
        engine.write_slot(res_id, 0, hash_hex, data)
        engine.commit_batch(res_id)

        assert bytes(engine.get(hash_hex)) == data

        engine.close()

    def test_expire_reservations_returns_zero_when_none(self, tmp_path):
        """expire_reservations on a fresh engine with no reservations returns 0."""
        engine = VolumeEngine(str(tmp_path / "volumes"), target_volume_size=TARGET_SIZE)

        expired = engine.expire_reservations()
        assert expired == 0

        engine.close()

    def test_multiple_reservations_independent(self, tmp_path):
        """Two preallocate calls produce independent reservations."""
        engine = VolumeEngine(str(tmp_path / "volumes"), target_volume_size=TARGET_SIZE)

        items_a = [make_data(i, 200) for i in range(5)]
        items_b = [make_data(i + 100, 300) for i in range(5)]

        sizes_a = [len(d) for _, d in items_a]
        sizes_b = [len(d) for _, d in items_b]

        res_a = engine.preallocate(sizes_a)
        res_b = engine.preallocate(sizes_b)

        assert res_a != res_b, "Reservation IDs must be unique"

        # Write and commit each independently
        for i, (h, d) in enumerate(items_a):
            engine.write_slot(res_a, i, h, d)
        engine.commit_batch(res_a)

        for i, (h, d) in enumerate(items_b):
            engine.write_slot(res_b, i, h, d)
        engine.commit_batch(res_b)

        # All items from both reservations should be readable
        for h, d in items_a + items_b:
            assert bytes(engine.get(h)) == d

        engine.close()

    def test_uncommitted_reservation_space_not_visible(self, tmp_path):
        """Preallocate + write_slot without commit: entries not visible (two-phase)."""
        engine = VolumeEngine(str(tmp_path / "volumes"), target_volume_size=TARGET_SIZE)

        items = [make_data(i, 500) for i in range(3)]
        sizes = [len(d) for _, d in items]
        res_id = engine.preallocate(sizes)

        # Write all slots but do NOT commit
        for i, (h, d) in enumerate(items):
            engine.write_slot(res_id, i, h, d)

        # Entries should NOT be visible yet (two-phase visibility, Decision #4A)
        for h, _ in items:
            assert not engine.exists(h), (
                f"exists() returned True before commit for {h[:16]}... "
                "(two-phase visibility violated)"
            )

        engine.close()


class TestCommitBatchSurvivesCloseAndRecovery:
    """Regression tests for Codex-reported bugs: commit_batch data must
    survive close() and be present in sealed volume TOC."""

    def test_3step_api_survives_close_reopen(self, tmp_path):
        """preallocate → write_slot → commit_batch → close → reopen: data survives.

        Regression for Codex bug #1: close()/Drop deleted batch-only volumes
        because entry_count() was 0 (commit_batch didn't add TocEntries).
        """
        import gc

        volumes_dir = tmp_path / "volumes"
        engine = VolumeEngine(str(volumes_dir), target_volume_size=TARGET_SIZE)

        items = [make_data(i, 500) for i in range(10)]
        sizes = [len(d) for _, d in items]
        res_id = engine.preallocate(sizes)
        for i, (h, d) in enumerate(items):
            engine.write_slot(res_id, i, h, d)
        engine.commit_batch(res_id)

        # Verify readable before close
        for h, _d in items:
            assert engine.exists(h), f"{h[:16]}... should exist before close"

        # Close and reopen
        engine.close()
        del engine
        gc.collect()

        engine2 = VolumeEngine(str(volumes_dir), target_volume_size=TARGET_SIZE)
        assert engine2.stats()["sealed_volume_count"] >= 1, "Volume should be sealed, not deleted"

        for h, d in items:
            assert engine2.exists(h), f"{h[:16]}... must survive close/reopen"
            assert bytes(engine2.read_content(h)) == d, f"Content mismatch for {h[:16]}..."
        engine2.close()

    def test_3step_api_entries_in_toc(self, tmp_path):
        """commit_batch entries must appear in sealed volume TOC.

        Regression for Codex bug #2: commit_batch only wrote redb/mem_index,
        not TocEntries. Sealed .vol had empty TOC → broke crash recovery
        and parse_volume_toc() used by tiering.
        """
        from nexus.services.volume_tiering import parse_volume_toc

        volumes_dir = tmp_path / "volumes"
        engine = VolumeEngine(str(volumes_dir), target_volume_size=TARGET_SIZE)

        items = [make_data(i, 200) for i in range(5)]
        sizes = [len(d) for _, d in items]
        res_id = engine.preallocate(sizes)
        for i, (h, d) in enumerate(items):
            engine.write_slot(res_id, i, h, d)
        engine.commit_batch(res_id)

        # Seal the active volume
        engine.seal_active()
        engine.close()

        # Parse TOC from sealed .vol — batch entries must be present
        vol_files = list(volumes_dir.glob("*.vol"))
        assert len(vol_files) >= 1, "Expected sealed .vol file"

        all_toc_hashes: set[str] = set()
        for vf in vol_files:
            toc = parse_volume_toc(vf)
            all_toc_hashes.update(toc.keys())

        for h, _ in items:
            assert h in all_toc_hashes, (
                f"Hash {h[:16]}... missing from sealed volume TOC. "
                "commit_batch must add TocEntries so tiering/recovery work."
            )

    def test_3step_api_crash_recovery_from_toc(self, tmp_path):
        """commit_batch entries must be recoverable from TOC after index loss.

        Simulates losing the redb index — recovery should rebuild from
        sealed volume TOCs, which must include batch-written entries.
        """
        import gc
        import os

        volumes_dir = tmp_path / "volumes"
        engine = VolumeEngine(str(volumes_dir), target_volume_size=TARGET_SIZE)

        items = [make_data(i, 300) for i in range(8)]
        sizes = [len(d) for _, d in items]
        res_id = engine.preallocate(sizes)
        for i, (h, d) in enumerate(items):
            engine.write_slot(res_id, i, h, d)
        engine.commit_batch(res_id)
        engine.seal_active()
        engine.close()
        del engine
        gc.collect()

        # Delete redb index to force TOC-based recovery
        redb_path = volumes_dir / "volume_index.redb"
        if redb_path.exists():
            os.remove(redb_path)
        # Also delete snapshot
        snap_path = volumes_dir / "mem_index.bin"
        if snap_path.exists():
            os.remove(snap_path)

        # Reopen — should recover from .vol TOCs
        engine2 = VolumeEngine(str(volumes_dir), target_volume_size=TARGET_SIZE)

        for h, d in items:
            assert engine2.exists(h), (
                f"Hash {h[:16]}... not recovered from TOC after index loss. "
                "commit_batch TocEntries must be in sealed volumes for recovery."
            )
            assert bytes(engine2.read_content(h)) == d
        engine2.close()
