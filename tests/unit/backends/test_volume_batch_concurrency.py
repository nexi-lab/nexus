"""Concurrency tests for batch pre-allocation (Issue #3409, Decision #9A).

Tests parallel write_slot calls, concurrent put + batch operations,
and read-during-commit scenarios to validate the batch pre-allocation
concurrency model.
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed

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
    """Generate a deterministic 64-char hex hash from an integer seed."""
    return hashlib.sha256(seed.to_bytes(4, "big")).hexdigest()


def make_data(seed: int, size: int = 100) -> bytes:
    """Generate deterministic data of a given size from a seed."""
    return bytes([seed % 256] * size)


# ─── Batch Pre-allocation Core ─────────────────────────────────────────────


class TestBatchPreallocation:
    """Core batch pre-allocation API tests: preallocate, write_slot, commit_batch."""

    def test_preallocate_returns_reservation_id(self, tmp_path):
        """preallocate(sizes=[100, 200, 300]) returns a positive u64 reservation ID."""
        engine = BlobPackEngine(str(tmp_path / "volumes"), target_volume_size=64 * 1024)
        res_id = engine.preallocate([100, 200, 300])
        assert isinstance(res_id, int), "reservation_id should be an integer"
        assert res_id > 0, "reservation_id should be positive"

    def test_preallocate_empty_raises(self, tmp_path):
        """preallocate([]) raises ValueError because zero slots is invalid."""
        engine = BlobPackEngine(str(tmp_path / "volumes"), target_volume_size=64 * 1024)
        with pytest.raises(ValueError, match="zero slots"):
            engine.preallocate([])

    def test_write_slot_and_commit(self, tmp_path):
        """Full roundtrip: preallocate -> write_slot x N -> commit_batch -> read_content."""
        engine = BlobPackEngine(str(tmp_path / "volumes"), target_volume_size=64 * 1024)

        sizes = [100, 200, 300]
        hashes = [make_hash(i) for i in range(len(sizes))]
        datas = [make_data(i, s) for i, s in enumerate(sizes)]

        res_id = engine.preallocate(sizes)

        for idx, (h, d) in enumerate(zip(hashes, datas)):
            engine.write_slot(res_id, idx, h, d)

        engine.commit_batch(res_id)

        # Verify all entries are readable
        for h, expected_data in zip(hashes, datas):
            result = engine.read_content(h)
            assert result is not None, f"Hash {h[:16]}... should be readable after commit"
            assert bytes(result) == expected_data, f"Data mismatch for hash {h[:16]}..."

    def test_filter_known_excludes_existing(self, tmp_path):
        """filter_known returns only hashes NOT already in the index."""
        engine = BlobPackEngine(str(tmp_path / "volumes"), target_volume_size=64 * 1024)

        # Put some hashes via the regular put() path
        known_hashes = [make_hash(i) for i in range(5)]
        for h in known_hashes:
            engine.put(h, b"existing data")

        # Mix known and unknown hashes
        unknown_hashes = [make_hash(i) for i in range(5, 10)]
        all_hashes = known_hashes + unknown_hashes

        filtered = engine.filter_known(all_hashes)

        assert set(filtered) == set(unknown_hashes), (
            "filter_known should return only unknown hashes"
        )
        for h in known_hashes:
            assert h not in filtered, f"Known hash {h[:16]}... should be excluded"

    def test_commit_batch_dedup(self, tmp_path):
        """If a hash is put() between preallocate and commit_batch, the duplicate is skipped."""
        engine = BlobPackEngine(str(tmp_path / "volumes"), target_volume_size=64 * 1024)

        h = make_hash(42)
        data_batch = make_data(42, 100)
        data_single = b"single put data!" + b"\x00" * 84  # different content, 100 bytes

        # Preallocate one slot
        res_id = engine.preallocate([100])
        engine.write_slot(res_id, 0, h, data_batch)

        # Race: put() the same hash before commit_batch
        engine.put(h, data_single)

        # commit_batch should skip the duplicate without error
        engine.commit_batch(res_id)

        # The data from the single put() should win (it was committed first)
        result = engine.read_content(h)
        assert result is not None
        assert bytes(result) == data_single, (
            "The put() data should be preserved; batch duplicate should be skipped"
        )

    def test_write_slot_wrong_size_raises(self, tmp_path):
        """write_slot with data size mismatching the reserved size raises ValueError."""
        engine = BlobPackEngine(str(tmp_path / "volumes"), target_volume_size=64 * 1024)

        res_id = engine.preallocate([100])
        h = make_hash(0)

        with pytest.raises(ValueError, match="size"):
            engine.write_slot(res_id, 0, h, make_data(0, 50))  # 50 != 100

    def test_write_slot_double_write_raises(self, tmp_path):
        """Writing to the same slot twice raises ValueError."""
        engine = BlobPackEngine(str(tmp_path / "volumes"), target_volume_size=64 * 1024)

        res_id = engine.preallocate([100])
        h = make_hash(0)
        data = make_data(0, 100)

        engine.write_slot(res_id, 0, h, data)

        with pytest.raises(ValueError, match="already written"):
            engine.write_slot(res_id, 0, h, data)

    def test_commit_batch_with_unwritten_slot_raises(self, tmp_path):
        """commit_batch with slots that were never written raises ValueError."""
        engine = BlobPackEngine(str(tmp_path / "volumes"), target_volume_size=64 * 1024)

        res_id = engine.preallocate([100, 200, 300])

        # Only write the first slot, leave slots 1 and 2 unwritten
        engine.write_slot(res_id, 0, make_hash(0), make_data(0, 100))

        with pytest.raises(ValueError, match="not written"):
            engine.commit_batch(res_id)

    def test_invalid_reservation_id_raises(self, tmp_path):
        """write_slot and commit_batch with a non-existent reservation ID raise ValueError."""
        engine = BlobPackEngine(str(tmp_path / "volumes"), target_volume_size=64 * 1024)

        bad_id = 999999

        with pytest.raises(ValueError, match="[Ii]nvalid"):
            engine.write_slot(bad_id, 0, make_hash(0), make_data(0, 100))

        with pytest.raises(ValueError, match="[Ii]nvalid"):
            engine.commit_batch(bad_id)


# ─── Parallel Write Slots ──────────────────────────────────────────────────


class TestParallelWriteSlots:
    """Concurrent write_slot calls to stress the lock-free pwrite path."""

    def test_parallel_write_slots(self, tmp_path):
        """4 threads writing 20 slots concurrently, then commit and verify all readable."""
        engine = BlobPackEngine(str(tmp_path / "volumes"), target_volume_size=64 * 1024)

        slot_count = 20
        data_size = 100
        sizes = [data_size] * slot_count
        hashes = [make_hash(i) for i in range(slot_count)]
        datas = [make_data(i, data_size) for i in range(slot_count)]

        res_id = engine.preallocate(sizes)

        # Write all slots in parallel
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = []
            for idx in range(slot_count):
                future = executor.submit(engine.write_slot, res_id, idx, hashes[idx], datas[idx])
                futures.append(future)

            # Ensure no exceptions
            for future in as_completed(futures):
                future.result()

        engine.commit_batch(res_id)

        # Verify all 20 entries are readable and correct
        for i in range(slot_count):
            result = engine.read_content(hashes[i])
            assert result is not None, f"Slot {i} should be readable after parallel commit"
            assert bytes(result) == datas[i], f"Data mismatch for slot {i}"

    def test_parallel_write_slots_large_batch(self, tmp_path):
        """100 slots across 8 workers — verify all readable with correct content."""
        engine = BlobPackEngine(str(tmp_path / "volumes"), target_volume_size=64 * 1024)

        slot_count = 100
        data_size = 200
        sizes = [data_size] * slot_count
        hashes = [make_hash(i) for i in range(slot_count)]
        datas = [make_data(i, data_size) for i in range(slot_count)]

        res_id = engine.preallocate(sizes)

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(engine.write_slot, res_id, idx, hashes[idx], datas[idx]): idx
                for idx in range(slot_count)
            }

            for future in as_completed(futures):
                slot_idx = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    pytest.fail(f"write_slot for slot {slot_idx} raised: {exc}")

        engine.commit_batch(res_id)

        # Verify all 100 entries
        for i in range(slot_count):
            result = engine.read_content(hashes[i])
            assert result is not None, f"Slot {i} (hash {hashes[i][:16]}...) missing"
            assert bytes(result) == datas[i], f"Content mismatch for slot {i}"

    def test_parallel_batch_and_single_put(self, tmp_path):
        """Batch write_slot and single put() run concurrently on disjoint hashes."""
        engine = BlobPackEngine(str(tmp_path / "volumes"), target_volume_size=64 * 1024)

        # Batch: seeds 0..9
        batch_count = 10
        data_size = 150
        batch_sizes = [data_size] * batch_count
        batch_hashes = [make_hash(i) for i in range(batch_count)]
        batch_datas = [make_data(i, data_size) for i in range(batch_count)]

        # Single puts: seeds 100..109 (disjoint from batch)
        single_count = 10
        single_hashes = [make_hash(100 + i) for i in range(single_count)]
        single_datas = [make_data(100 + i, data_size) for i in range(single_count)]

        res_id = engine.preallocate(batch_sizes)

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = []

            # Submit batch write_slot calls
            for idx in range(batch_count):
                futures.append(
                    executor.submit(
                        engine.write_slot, res_id, idx, batch_hashes[idx], batch_datas[idx]
                    )
                )

            # Submit single put() calls concurrently
            for i in range(single_count):
                futures.append(executor.submit(engine.put, single_hashes[i], single_datas[i]))

            for future in as_completed(futures):
                future.result()

        engine.commit_batch(res_id)

        # Verify batch entries
        for i in range(batch_count):
            result = engine.read_content(batch_hashes[i])
            assert result is not None, f"Batch slot {i} should be readable"
            assert bytes(result) == batch_datas[i], f"Batch slot {i} data mismatch"

        # Verify single-put entries
        for i in range(single_count):
            result = engine.read_content(single_hashes[i])
            assert result is not None, f"Single put {i} should be readable"
            assert bytes(result) == single_datas[i], f"Single put {i} data mismatch"


# ─── Expired Reservations ──────────────────────────────────────────────────


class TestExpiredReservations:
    """Tests for reservation expiry and cleanup via expire_reservations()."""

    def test_reservation_expires_returns_zero_for_fresh(self, tmp_path):
        """expire_reservations returns 0 for a fresh (non-expired) reservation.

        Since RESERVATION_TIMEOUT_SECS is 60s and we cannot control it from
        Python, we verify that a freshly created reservation is NOT expired.
        """
        engine = BlobPackEngine(str(tmp_path / "volumes"), target_volume_size=64 * 1024)

        res_id = engine.preallocate([100, 200])

        # Immediately call expire — nothing should be expired
        expired_count = engine.expire_reservations()
        assert expired_count == 0, "Fresh reservation should not be expired"

        # Reservation should still be usable
        engine.write_slot(res_id, 0, make_hash(0), make_data(0, 100))
        engine.write_slot(res_id, 1, make_hash(1), make_data(1, 200))
        engine.commit_batch(res_id)

        # Verify data is readable
        assert engine.read_content(make_hash(0)) is not None
        assert engine.read_content(make_hash(1)) is not None

    def test_expire_reservations_cleanup(self, tmp_path):
        """expire_reservations cleans up committed/consumed reservations correctly.

        After commit_batch consumes a reservation, expire_reservations should
        not count it (it was already removed). Creating multiple reservations
        and committing some verifies the cleanup path.
        """
        engine = BlobPackEngine(str(tmp_path / "volumes"), target_volume_size=64 * 1024)

        # Create and commit reservation 1
        res_id1 = engine.preallocate([100])
        engine.write_slot(res_id1, 0, make_hash(0), make_data(0, 100))
        engine.commit_batch(res_id1)

        # Create reservation 2 but leave it uncommitted
        res_id2 = engine.preallocate([200])
        engine.write_slot(res_id2, 0, make_hash(1), make_data(1, 200))

        # expire_reservations should return 0 — res1 is consumed, res2 is fresh
        expired_count = engine.expire_reservations()
        assert expired_count == 0, (
            "No reservations should be expired: res1 was committed, res2 is fresh"
        )

        # res2 should still be valid after expire call
        engine.commit_batch(res_id2)
        assert engine.read_content(make_hash(1)) is not None

        # Now all reservations are consumed — expire should still return 0
        expired_count = engine.expire_reservations()
        assert expired_count == 0, "No reservations remain to expire after all are committed"
