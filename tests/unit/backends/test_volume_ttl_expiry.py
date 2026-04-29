"""Tests for TTL volume expiry (Issue #3405).

Tests the BlobPackEngine.expire_ttl_volumes() and put_with_expiry() methods,
read-time expiry checks, and the BlobPackLocalTransport TTL routing layer.
"""

from __future__ import annotations

import time

import pytest


def make_hash(seed: int) -> str:
    return f"{seed:064x}"


def _vol_engine_available() -> bool:
    try:
        from nexus_runtime import BlobPackEngine  # noqa: F401

        return True
    except ImportError:
        return False


needs_vol_engine = pytest.mark.skipif(
    not _vol_engine_available(), reason="nexus_runtime.BlobPackEngine not available"
)


@needs_vol_engine
class TestPutWithExpiry:
    """Test writing blobs with expiry timestamps."""

    def test_put_with_expiry_returns_true(self, tmp_path) -> None:
        from nexus_runtime import BlobPackEngine

        engine = BlobPackEngine(str(tmp_path / "vol"))
        result = engine.put_with_expiry(make_hash(1), b"hello", time.time() + 3600)
        assert result is True

    def test_put_with_expiry_dedup(self, tmp_path) -> None:
        from nexus_runtime import BlobPackEngine

        engine = BlobPackEngine(str(tmp_path / "vol"))
        h = make_hash(1)
        expiry = time.time() + 3600
        assert engine.put_with_expiry(h, b"hello", expiry) is True
        assert engine.put_with_expiry(h, b"hello", expiry) is False  # dedup

    def test_put_with_zero_expiry_is_permanent(self, tmp_path) -> None:
        from nexus_runtime import BlobPackEngine

        engine = BlobPackEngine(str(tmp_path / "vol"))
        h = make_hash(1)
        engine.put_with_expiry(h, b"permanent", 0.0)
        # Should be readable forever
        assert engine.read_content(h) is not None
        engine.close()

    def test_read_expired_entry_returns_none(self, tmp_path) -> None:
        from nexus_runtime import BlobPackEngine

        engine = BlobPackEngine(str(tmp_path / "vol"))
        h = make_hash(1)
        # Set expiry 1 second in the past
        past_expiry = time.time() - 1.0
        engine.put_with_expiry(h, b"expired", past_expiry)
        # read_content should return None (entry is expired)
        assert engine.read_content(h) is None
        engine.close()

    def test_exists_expired_entry_returns_false(self, tmp_path) -> None:
        from nexus_runtime import BlobPackEngine

        engine = BlobPackEngine(str(tmp_path / "vol"))
        h = make_hash(1)
        engine.put_with_expiry(h, b"expired", time.time() - 1.0)
        assert engine.exists(h) is False
        engine.close()

    def test_get_size_expired_entry_returns_none(self, tmp_path) -> None:
        from nexus_runtime import BlobPackEngine

        engine = BlobPackEngine(str(tmp_path / "vol"))
        h = make_hash(1)
        engine.put_with_expiry(h, b"expired", time.time() - 1.0)
        assert engine.get_size(h) is None
        engine.close()

    def test_unexpired_entry_readable(self, tmp_path) -> None:
        from nexus_runtime import BlobPackEngine

        engine = BlobPackEngine(str(tmp_path / "vol"))
        h = make_hash(1)
        engine.put_with_expiry(h, b"still alive", time.time() + 3600)
        data = engine.read_content(h)
        assert data is not None
        assert bytes(data) == b"still alive"
        engine.close()

    def test_mix_permanent_and_ttl(self, tmp_path) -> None:
        from nexus_runtime import BlobPackEngine

        engine = BlobPackEngine(str(tmp_path / "vol"))
        h_perm = make_hash(1)
        h_ttl = make_hash(2)
        h_expired = make_hash(3)

        engine.put(h_perm, b"permanent")
        engine.put_with_expiry(h_ttl, b"ttl", time.time() + 3600)
        engine.put_with_expiry(h_expired, b"expired", time.time() - 1.0)

        assert engine.read_content(h_perm) is not None
        assert engine.read_content(h_ttl) is not None
        assert engine.read_content(h_expired) is None  # expired
        engine.close()


@needs_vol_engine
class TestExpireTTLVolumes:
    """Test the expire_ttl_volumes() method."""

    def test_expire_empty_engine(self, tmp_path) -> None:
        from nexus_runtime import BlobPackEngine

        engine = BlobPackEngine(str(tmp_path / "vol"))
        result = engine.expire_ttl_volumes()
        assert result == []
        engine.close()

    def test_expire_permanent_entries_untouched(self, tmp_path) -> None:
        from nexus_runtime import BlobPackEngine

        engine = BlobPackEngine(str(tmp_path / "vol"))
        h = make_hash(1)
        engine.put(h, b"permanent data")
        engine.seal_active()

        result = engine.expire_ttl_volumes()
        assert result == []  # permanent entries not expired
        assert engine.read_content(h) is not None
        engine.close()

    def test_expire_removes_expired_entries(self, tmp_path) -> None:
        from nexus_runtime import BlobPackEngine

        engine = BlobPackEngine(str(tmp_path / "vol"))
        hashes = []
        for i in range(10):
            h = make_hash(i)
            hashes.append(h)
            engine.put_with_expiry(h, f"data_{i}".encode(), time.time() - 1.0)

        engine.seal_active()
        result = engine.expire_ttl_volumes()

        total_expired = sum(count for _, count in result)
        assert total_expired == 10

        # All entries should be gone
        for h in hashes:
            assert engine.read_content(h) is None

        engine.close()

    def test_expire_keeps_unexpired_entries(self, tmp_path) -> None:
        """Volume-level expiry: volume not deleted until ALL entries expire.

        Even though one entry is past expiry, the volume's max_expiry is
        the live entry's expiry (future). The volume stays. The expired
        entry is still invisible at read-time (mem_index expiry check).
        """
        from nexus_runtime import BlobPackEngine

        engine = BlobPackEngine(str(tmp_path / "vol"))

        h_expired = make_hash(1)
        h_live = make_hash(2)

        engine.put_with_expiry(h_expired, b"expired", time.time() - 1.0)
        engine.put_with_expiry(h_live, b"live", time.time() + 3600)
        engine.seal_active()

        # Volume not expired yet — max_expiry is the live entry's expiry
        result = engine.expire_ttl_volumes()
        assert result == []  # no volumes expired

        # But expired entry is invisible at read-time
        assert engine.read_content(h_expired) is None
        # Live entry is still readable
        assert engine.read_content(h_live) is not None
        engine.close()

    def test_expire_deletes_fully_empty_volume_file(self, tmp_path) -> None:
        from nexus_runtime import BlobPackEngine

        vol_dir = tmp_path / "vol"
        engine = BlobPackEngine(str(vol_dir))

        # Write expired entries and seal
        for i in range(5):
            engine.put_with_expiry(make_hash(i), b"data", time.time() - 1.0)
        engine.seal_active()

        # Count .vol files before expiry
        vol_files_before = list(vol_dir.glob("*.vol"))
        assert len(vol_files_before) > 0

        engine.expire_ttl_volumes()

        # Volume file should be deleted
        vol_files_after = list(vol_dir.glob("*.vol"))
        assert len(vol_files_after) < len(vol_files_before)

        engine.close()

    def test_expire_idempotent(self, tmp_path) -> None:
        from nexus_runtime import BlobPackEngine

        engine = BlobPackEngine(str(tmp_path / "vol"))
        engine.put_with_expiry(make_hash(1), b"data", time.time() - 1.0)
        engine.seal_active()

        result1 = engine.expire_ttl_volumes()
        result2 = engine.expire_ttl_volumes()  # second call should be no-op

        assert sum(c for _, c in result1) == 1
        assert result2 == []
        engine.close()


@needs_vol_engine
class TestSealIfNonempty:
    """Test the seal_if_nonempty() method for TTL rotation."""

    def test_seal_empty_returns_false(self, tmp_path) -> None:
        from nexus_runtime import BlobPackEngine

        engine = BlobPackEngine(str(tmp_path / "vol"))
        assert engine.seal_if_nonempty() is False
        engine.close()

    def test_seal_nonempty_returns_true(self, tmp_path) -> None:
        from nexus_runtime import BlobPackEngine

        engine = BlobPackEngine(str(tmp_path / "vol"))
        engine.put(make_hash(1), b"data")
        assert engine.seal_if_nonempty() is True

        # Should be readable after seal
        assert engine.read_content(make_hash(1)) is not None
        engine.close()

    def test_seal_after_seal_returns_false(self, tmp_path) -> None:
        from nexus_runtime import BlobPackEngine

        engine = BlobPackEngine(str(tmp_path / "vol"))
        engine.put(make_hash(1), b"data")
        engine.seal_if_nonempty()

        # No new data — should return False
        assert engine.seal_if_nonempty() is False
        engine.close()


@needs_vol_engine
class TestSnapshotWithExpiry:
    """Test that snapshot persistence includes expiry field."""

    def test_snapshot_roundtrip_with_expiry(self, tmp_path) -> None:
        from nexus_runtime import BlobPackEngine

        vol_dir = tmp_path / "vol"

        # Write entries with expiry
        engine = BlobPackEngine(str(vol_dir))
        future = time.time() + 3600
        engine.put_with_expiry(make_hash(1), b"ttl_data", future)
        engine.put(make_hash(2), b"permanent")
        engine.seal_active()
        engine.close()
        del engine  # ensure redb lock is released

        # Re-open — should load from snapshot
        engine2 = BlobPackEngine(str(vol_dir))

        # TTL entry should still be readable (not expired)
        data = engine2.read_content(make_hash(1))
        assert data is not None
        assert bytes(data) == b"ttl_data"

        # Permanent entry should be readable
        data2 = engine2.read_content(make_hash(2))
        assert data2 is not None
        assert bytes(data2) == b"permanent"

        engine2.close()


class TestTransportTTLRouting:
    """Test BlobPackLocalTransport TTL routing (Python layer)."""

    def test_store_ttl_routes_to_bucket(self, tmp_path) -> None:
        if not _vol_engine_available():
            pytest.skip("BlobPackEngine not available")

        from nexus.backends.transports.blob_pack_local_transport import BlobPackLocalTransport

        transport = BlobPackLocalTransport(str(tmp_path))
        h = make_hash(1)
        key = f"cas/{h[:2]}/{h[2:4]}/{h}"

        transport.store_ttl(key, b"ttl_data", ttl_seconds=60.0)

        # Should have created a TTL engine
        assert transport.ttl_engine_count >= 1

        # Should be readable
        data, _ = transport.fetch(key)
        assert data == b"ttl_data"

        transport.close()

    def test_store_ttl_large_ttl_goes_permanent(self, tmp_path) -> None:
        if not _vol_engine_available():
            pytest.skip("BlobPackEngine not available")

        from nexus.backends.transports.blob_pack_local_transport import BlobPackLocalTransport

        transport = BlobPackLocalTransport(str(tmp_path))
        h = make_hash(1)
        key = f"cas/{h[:2]}/{h[2:4]}/{h}"

        # TTL exceeds all buckets → should go to permanent engine
        transport.store_ttl(key, b"permanent", ttl_seconds=9999999.0)

        # No TTL engines should be created
        assert transport.ttl_engine_count == 0

        data, _ = transport.fetch(key)
        assert data == b"permanent"

        transport.close()

    def test_expire_ttl_volumes_via_transport(self, tmp_path) -> None:
        if not _vol_engine_available():
            pytest.skip("BlobPackEngine not available")

        from nexus.backends.transports.blob_pack_local_transport import BlobPackLocalTransport

        transport = BlobPackLocalTransport(str(tmp_path))
        h = make_hash(1)
        key = f"cas/{h[:2]}/{h[2:4]}/{h}"

        # Write with very short TTL (already expired)
        transport.store_ttl(key, b"expired", ttl_seconds=0.001)
        # Force seal so expiry can operate on sealed volumes
        for engine in transport._ttl_engines.values():
            engine.seal_active()

        # Wait a tiny bit to ensure expiry
        time.sleep(0.01)

        results = transport.expire_ttl_volumes()
        total = sum(count for _, count in results)
        assert total >= 1

        transport.close()
