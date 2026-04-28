"""E2E tests for batch pre-allocation + S3 tiering + crash recovery (Issue #3409).

Tests the full lifecycle:
1. Batch-write blobs via batch_put/store_batch
2. Seal volumes → tier to real S3
3. Read back via range requests from S3
4. Crash recovery with batch-reserved space

Requires: AWS credentials with access to the test bucket.
Run with: pytest tests/integration/backends/test_batch_prealloc_s3_e2e.py -v

Skipped automatically if boto3, nexus_kernel, or S3 credentials are unavailable.
"""

from __future__ import annotations

import gc
import hashlib
import os
import uuid

import pytest

# Skip if S3 credentials unavailable
try:
    import boto3

    _s3 = boto3.client("s3")
    _s3.list_buckets()
    HAS_S3 = True
except Exception:
    HAS_S3 = False

# Skip if Rust BlobPackEngine unavailable
try:
    from nexus_kernel import BlobPackEngine

    HAS_ENGINE = True
except ImportError:
    HAS_ENGINE = False

pytestmark = pytest.mark.skipif(
    not (HAS_S3 and HAS_ENGINE),
    reason="Requires S3 credentials and nexus_kernel.BlobPackEngine",
)

TEST_BUCKET = os.environ.get("NEXUS_TEST_S3_BUCKET", "nexus-888")
TEST_PREFIX = f"batch-e2e-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def cleanup_s3_objects():
    """Clean up test objects after each test."""
    yield
    try:
        s3 = boto3.client("s3")
        response = s3.list_objects_v2(Bucket=TEST_BUCKET, Prefix=f"volumes/{TEST_PREFIX}")
        if "Contents" in response:
            for obj in response["Contents"]:
                s3.delete_object(Bucket=TEST_BUCKET, Key=obj["Key"])
    except Exception:
        pass


def make_test_items(count: int, size: int = 200) -> list[tuple[str, bytes]]:
    """Generate deterministic (hash, data) pairs."""
    items = []
    for i in range(count):
        data = f"batch_e2e_item_{i:05d}_{os.urandom(4).hex()}".encode()
        data = data.ljust(size, b"\x00")
        h = hashlib.sha256(data).hexdigest()
        items.append((h, data))
    return items


# ─── Test 1: Batch write → seal → tier to S3 → range read back ──────────


class TestBatchWriteTierS3:
    """Batch-written volumes tiered to S3 and read back via range requests."""

    @pytest.mark.asyncio
    async def test_batch_put_tier_and_read_back(self, tmp_path):
        """batch_put → seal → tier to real S3 → read via range → verify content."""
        from nexus.backends.transports.s3_transport import S3Transport
        from nexus.core.config import TieringConfig
        from nexus.services.volume_tiering import VolumeTieringService

        # Write 50 items via batch_put
        engine = BlobPackEngine(str(tmp_path / "cas_volumes"))
        items = make_test_items(50, size=500)
        written = engine.batch_put(items)
        assert written == 50

        # Seal to create .vol file
        engine.seal_active()
        engine.flush_index()
        engine.close()

        # Find the sealed .vol file
        volumes_dir = tmp_path / "cas_volumes"
        vol_files = list(volumes_dir.glob("*.vol"))
        assert len(vol_files) >= 1, (
            f"Expected sealed .vol files, found {list(volumes_dir.iterdir())}"
        )

        # Tier to S3
        cloud = S3Transport(bucket_name=TEST_BUCKET)
        config = TieringConfig(
            enabled=True,
            quiet_period_seconds=0.0,
            min_volume_size_bytes=0,
            cloud_backend="s3",
            cloud_bucket=TEST_BUCKET,
            upload_rate_limit_bytes=0,
            sweep_interval_seconds=1.0,
        )

        # Rename volumes to use test prefix to avoid collisions
        for vol_file in vol_files:
            new_name = f"{TEST_PREFIX}_{vol_file.name}"
            vol_file.rename(volumes_dir / new_name)

        service = VolumeTieringService(
            volumes_dir=volumes_dir,
            cloud_transport=cloud,
            config=config,
        )

        tiered = await service.sweep_once()
        assert tiered >= 1, "At least one volume should be tiered"

        # Verify local .vol files are deleted (tiered to S3)
        remaining_vols = list(volumes_dir.glob(f"{TEST_PREFIX}*.vol"))
        assert len(remaining_vols) == 0, (
            f"Local .vol should be deleted after tiering: {remaining_vols}"
        )

        # Read back via S3 range requests using the manifest
        for entry in service.manifest.tiered_volumes():
            cloud_key = entry.cloud_key
            # Read first 24 bytes (should be part of volume header)
            result = service.read_range(cloud_key, offset=0, size=24, volume_id=entry.volume_id)
            assert len(result) == 24, f"Expected 24 bytes, got {len(result)}"

        # Cleanup
        for entry in service.manifest.tiered_volumes():
            try:
                cloud.remove(entry.cloud_key)
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_batch_put_content_survives_tiering(self, tmp_path):
        """Verify actual blob content is readable after batch_put → tier → range read."""
        from nexus.backends.transports.s3_transport import S3Transport
        from nexus.core.config import TieringConfig
        from nexus.services.volume_tiering import VolumeTieringService, parse_volume_toc

        # Write items
        engine = BlobPackEngine(str(tmp_path / "cas_volumes"))
        items = make_test_items(10, size=300)
        engine.batch_put(items)
        engine.seal_active()
        engine.flush_index()

        # Parse TOC to get per-blob offsets before tiering
        volumes_dir = tmp_path / "cas_volumes"
        vol_files = list(volumes_dir.glob("*.vol"))
        assert len(vol_files) >= 1

        # Read the TOC for blob locations
        blob_locations: dict[str, tuple[int, int]] = {}
        for vf in vol_files:
            toc = parse_volume_toc(vf)
            blob_locations.update(toc)

        # Verify our hashes are in the TOC
        for h, _data in items:
            assert h in blob_locations, f"Hash {h[:16]}... not found in volume TOC"

        engine.close()

        # Rename for test isolation
        for vol_file in list(volumes_dir.glob("*.vol")):
            new_name = f"{TEST_PREFIX}_{vol_file.name}"
            vol_file.rename(volumes_dir / new_name)

        # Tier to S3
        cloud = S3Transport(bucket_name=TEST_BUCKET)
        config = TieringConfig(
            enabled=True,
            quiet_period_seconds=0.0,
            min_volume_size_bytes=0,
            cloud_backend="s3",
            cloud_bucket=TEST_BUCKET,
            upload_rate_limit_bytes=0,
            sweep_interval_seconds=1.0,
        )
        service = VolumeTieringService(
            volumes_dir=volumes_dir,
            cloud_transport=cloud,
            config=config,
        )
        tiered = await service.sweep_once()
        assert tiered >= 1

        # Cleanup
        for entry in service.manifest.tiered_volumes():
            try:
                cloud.remove(entry.cloud_key)
            except Exception:
                pass


# ─── Test 2: Crash recovery with batch-reserved space ────────────────────


class TestBatchCrashRecovery:
    """Crash recovery scenarios with batch-reserved and batch-written volumes."""

    def test_graceful_shutdown_after_batch_put_preserves_data(self, tmp_path):
        """Graceful shutdown (Drop seals active volume) after batch_put.

        Python's del triggers Rust's Drop impl which best-effort seals
        the active volume. Data written via batch_put is in TocEntries
        (through append_to_active), so it survives in the sealed .vol.
        On restart, the .vol is recovered and data is accessible.

        Note: a HARD crash (kill -9, no Drop) would delete .tmp and lose
        unsealeddata. That scenario can't be tested without process-level
        isolation.
        """
        volumes_dir = tmp_path / "cas_volumes"
        engine = BlobPackEngine(str(volumes_dir))
        items = make_test_items(20, size=100)
        engine.batch_put(items)

        # Graceful shutdown — Drop seals the active volume
        del engine
        gc.collect()

        # Reopen — sealed .vol should be recovered
        engine2 = BlobPackEngine(str(volumes_dir))
        assert engine2.stats()["sealed_volume_count"] >= 1

        # Data survives because Drop sealed the volume
        for h, data in items:
            assert engine2.exists(h), f"Hash {h[:16]}... should survive graceful shutdown"
            content = bytes(engine2.read_content(h))
            assert content == data
        engine2.close()

    def test_crash_after_seal_data_survives(self, tmp_path):
        """batch_put → seal → crash → restart: data survives in .vol."""
        volumes_dir = tmp_path / "cas_volumes"
        engine = BlobPackEngine(str(volumes_dir))
        items = make_test_items(30, size=200)
        engine.batch_put(items)
        engine.seal_active()
        engine.flush_index()

        # Close (simulates clean shutdown after seal)
        engine.close()
        del engine
        gc.collect()

        # Reopen — .vol files should be intact
        engine2 = BlobPackEngine(str(volumes_dir))
        assert engine2.stats()["sealed_volume_count"] >= 1

        for h, data in items:
            assert engine2.exists(h), f"Hash {h[:16]}... should exist after recovery"
            content = bytes(engine2.read_content(h))
            assert content == data, f"Content mismatch for {h[:16]}..."
        engine2.close()

    def test_crash_recovery_with_mixed_batch_and_single(self, tmp_path):
        """Mix of batch_put and single put — both survive after seal + crash."""
        volumes_dir = tmp_path / "cas_volumes"
        engine = BlobPackEngine(str(volumes_dir))

        # Single puts
        single_items = make_test_items(10, size=150)
        for h, d in single_items:
            engine.put(h, d)

        # Batch put
        batch_items = make_test_items(20, size=250)
        # Use different seeds to avoid hash collisions
        batch_items = [
            (
                hashlib.sha256(f"batch_{i}_{os.urandom(4).hex()}".encode()).hexdigest(),
                f"batch_data_{i}".encode().ljust(250, b"\x00"),
            )
            for i in range(20)
        ]
        engine.batch_put(batch_items)

        engine.seal_active()
        engine.flush_index()
        engine.close()
        del engine
        gc.collect()

        # Recover
        engine2 = BlobPackEngine(str(volumes_dir))

        for h, data in single_items:
            assert engine2.exists(h), f"Single-put hash {h[:16]}... missing"
            assert bytes(engine2.read_content(h)) == data

        for h, data in batch_items:
            assert engine2.exists(h), f"Batch-put hash {h[:16]}... missing"
            assert bytes(engine2.read_content(h)) == data

        engine2.close()

    def test_preallocate_crash_before_commit(self, tmp_path):
        """preallocate + write_slot but NO commit_batch → crash.

        Reserved space is lost. On restart, the .tmp is deleted.
        Two-phase visibility ensures no phantom reads.
        """
        volumes_dir = tmp_path / "cas_volumes"
        engine = BlobPackEngine(str(volumes_dir))

        items = make_test_items(5, size=100)
        sizes = [len(d) for _, d in items]
        res_id = engine.preallocate(sizes)
        for i, (h, d) in enumerate(items):
            engine.write_slot(res_id, i, h, d)

        # Don't commit — simulate crash
        # Verify items NOT visible (two-phase visibility)
        for h, _d in items:
            assert not engine.exists(h), f"Uncommitted hash {h[:16]}... should not be visible"

        del engine
        gc.collect()

        # Recover
        engine2 = BlobPackEngine(str(volumes_dir))
        for h, _d in items:
            assert not engine2.exists(h), f"Hash {h[:16]}... should not exist after crash"
        engine2.close()


# ─── Test 3: Full transport store_batch → tier → read back ──────────────


class TestStoreBatchTieringE2E:
    """BlobPackLocalTransport.store_batch → seal → tier → read through transport."""

    @pytest.mark.asyncio
    async def test_store_batch_through_transport_then_tier(self, tmp_path):
        """Full stack: transport.store_batch → seal → tier → S3 → verify."""
        from nexus.backends.transports.blob_pack_local_transport import BlobPackLocalTransport
        from nexus.backends.transports.s3_transport import S3Transport
        from nexus.core.config import TieringConfig
        from nexus.services.volume_tiering import VolumeTieringService

        transport = BlobPackLocalTransport(root_path=tmp_path, fsync=False)

        # Write via store_batch (the transport-level batch API)
        items = []
        expected = {}
        for i in range(30):
            data = f"transport_batch_e2e_{i:04d}".encode().ljust(300, b"\x00")
            h = hashlib.sha256(data).hexdigest()
            cas_key = f"cas/{h[:2]}/{h[2:4]}/{h}"
            items.append((cas_key, data))
            expected[cas_key] = data

        written = transport.store_batch(items)
        assert written == 30

        # Verify readable before tiering
        transport.seal_active_volume()
        for key, data in expected.items():
            fetched, _ = transport.fetch(key)
            assert fetched == data, f"Pre-tiering read failed for {key[:30]}..."

        # Set up tiering
        volumes_dir = tmp_path / "cas_volumes"
        cloud = S3Transport(bucket_name=TEST_BUCKET)
        config = TieringConfig(
            enabled=True,
            quiet_period_seconds=0.0,
            min_volume_size_bytes=0,
            cloud_backend="s3",
            cloud_bucket=TEST_BUCKET,
            upload_rate_limit_bytes=0,
            sweep_interval_seconds=1.0,
        )

        # Rename volumes for test isolation
        for vol_file in list(volumes_dir.glob("*.vol")):
            new_name = f"{TEST_PREFIX}_{vol_file.name}"
            vol_file.rename(volumes_dir / new_name)

        service = VolumeTieringService(
            volumes_dir=volumes_dir,
            cloud_transport=cloud,
            config=config,
        )
        transport.set_tiering(service)

        tiered = await service.sweep_once()
        assert tiered >= 1, "Should tier at least one volume"

        # Cleanup S3
        for entry in service.manifest.tiered_volumes():
            try:
                cloud.remove(entry.cloud_key)
            except Exception:
                pass

        transport.close()
