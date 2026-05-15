"""Real S3 integration tests for volume-level cold tiering (Issue #3406).

End-to-end: write blobs → seal → tier to S3 → read via range request → verify.
Uses real AWS credentials from ~/.aws/credentials or environment.

Run with: pytest tests/integration/backends/test_volume_tiering_s3.py -v
Requires: AWS credentials with access to the test bucket.

Skipped automatically if boto3 is unavailable or credentials are missing.
"""

from __future__ import annotations

import os
import uuid

import pytest

# Skip entire module if boto3 or credentials unavailable
try:
    import boto3

    _s3 = boto3.client("s3")
    # Quick check that credentials work (will fail fast if not)
    _s3.list_buckets()
    HAS_S3 = True
except Exception:
    HAS_S3 = False

pytestmark = pytest.mark.skipif(not HAS_S3, reason="S3 credentials not available")

# Mock TOC parser for integration tests that use fake volume data
_MOCK_TOC = {"deadbeef" * 8: (0, 100)}


@pytest.fixture(autouse=True)
def _mock_toc_parser(monkeypatch):
    """Patch parse_volume_toc so tests don't need real .vol binary format."""
    monkeypatch.setattr(
        "nexus.services.volume_tiering.parse_volume_toc",
        lambda path: _MOCK_TOC,
    )


# Test bucket — override via NEXUS_TEST_S3_BUCKET env var
TEST_BUCKET = os.environ.get("NEXUS_TEST_S3_BUCKET", "nexus-888")
# Prefix to isolate test objects (cleaned up after each test)
TEST_PREFIX = f"tiering-test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def s3_transport():
    """Create a real S3Transport for testing."""
    from nexus.backends.transports.s3_transport import S3Transport

    return S3Transport(bucket_name=TEST_BUCKET)


@pytest.fixture(autouse=True)
def cleanup_s3_objects():
    """Clean up test objects after each test."""
    yield
    # Delete all objects under the test prefix
    try:
        s3 = boto3.client("s3")
        response = s3.list_objects_v2(Bucket=TEST_BUCKET, Prefix=f"volumes/{TEST_PREFIX}")
        if "Contents" in response:
            for obj in response["Contents"]:
                s3.delete_object(Bucket=TEST_BUCKET, Key=obj["Key"])
    except Exception:
        pass


class TestS3RangeRead:
    """Test get_blob_range() against real S3."""

    def test_range_read_middle_of_object(self, s3_transport):
        """Upload an object, then read a byte range from the middle."""
        key = f"volumes/{TEST_PREFIX}/range_test.dat"
        data = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ" * 100  # 2600 bytes

        s3_transport.store(key, data)

        # Range read from middle
        result = s3_transport.get_blob_range(key, offset=100, size=26)
        assert result == data[100:126]

    def test_range_read_start(self, s3_transport):
        key = f"volumes/{TEST_PREFIX}/range_start.dat"
        data = b"HELLO WORLD" * 50

        s3_transport.store(key, data)
        result = s3_transport.get_blob_range(key, offset=0, size=5)
        assert result == b"HELLO"

    def test_range_read_end(self, s3_transport):
        key = f"volumes/{TEST_PREFIX}/range_end.dat"
        data = b"0123456789"

        s3_transport.store(key, data)
        result = s3_transport.get_blob_range(key, offset=7, size=3)
        assert result == b"789"

    def test_range_read_single_byte(self, s3_transport):
        key = f"volumes/{TEST_PREFIX}/range_single.dat"
        data = b"ABCDEF"

        s3_transport.store(key, data)
        result = s3_transport.get_blob_range(key, offset=2, size=1)
        assert result == b"C"


class TestS3UploadFile:
    """Test upload_file() against real S3."""

    def test_upload_and_verify(self, s3_transport, tmp_path):
        """Upload a local file and verify it matches."""
        key = f"volumes/{TEST_PREFIX}/upload_test.dat"
        local_data = b"volume content for upload test " * 1000  # ~30KB

        local_file = tmp_path / "test_volume.dat"
        local_file.write_bytes(local_data)

        s3_transport.upload_file(key, str(local_file))

        # Verify full download matches
        downloaded, _ = s3_transport.fetch(key)
        assert downloaded == local_data

        # Verify size
        size = s3_transport.get_size(key)
        assert size == len(local_data)


class TestTieringE2EWithS3:
    """Full end-to-end: tiering service → real S3 → range read back."""

    @pytest.mark.asyncio
    async def test_tier_and_read_back(self, tmp_path):
        """Write blobs, seal, tier to S3, read via range request."""
        from nexus.backends.transports.s3_transport import S3Transport
        from nexus.core.config import TieringConfig
        from nexus.services.volume_tiering import VolumeTieringService

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

        volumes_dir = tmp_path / "cas_volumes"
        volumes_dir.mkdir()

        # Create a fake sealed volume .vol file
        volume_id = f"{TEST_PREFIX}_vol001"
        volume_data = b"This is the volume content with some interesting data! " * 100
        vol_path = volumes_dir / f"{volume_id}.vol"
        vol_path.write_bytes(volume_data)

        service = VolumeTieringService(
            volumes_dir=volumes_dir,
            cloud_transport=cloud,
            config=config,
        )

        # Tier the volume
        tiered = await service.sweep_once()
        assert tiered == 1

        # Verify manifest says TIERED
        entry = service.manifest.get(volume_id)
        assert entry is not None
        assert entry.state == "tiered"
        assert entry.checksum_sha256 != ""

        # Local .vol should be gone
        assert not vol_path.exists()

        # Read back via range request — should hit real S3
        cloud_key = f"volumes/{volume_id}.vol"
        result = service.read_range(cloud_key, offset=0, size=55, volume_id=volume_id)
        assert result == volume_data[:55]

        # Read from middle
        result2 = service.read_range(cloud_key, offset=100, size=50, volume_id=volume_id)
        assert result2 == volume_data[100:150]

        # Cleanup S3
        try:
            cloud.remove(cloud_key)
        except Exception:
            pass

    @pytest.mark.asyncio
    async def test_tier_read_burst_rehydrate(self, tmp_path):
        """Full lifecycle: tier → burst reads → cache promotion → rehydrate."""
        from nexus.backends.transports.s3_transport import S3Transport
        from nexus.core.config import TieringConfig
        from nexus.services.volume_tiering import VolumeTieringService

        cloud = S3Transport(bucket_name=TEST_BUCKET)
        config = TieringConfig(
            enabled=True,
            quiet_period_seconds=0.0,
            min_volume_size_bytes=0,
            cloud_backend="s3",
            cloud_bucket=TEST_BUCKET,
            upload_rate_limit_bytes=0,
            sweep_interval_seconds=1.0,
            burst_read_threshold=3,
            burst_read_window_seconds=60.0,
        )

        volumes_dir = tmp_path / "cas_volumes"
        volumes_dir.mkdir()

        volume_id = f"{TEST_PREFIX}_vol002"
        volume_data = b"Burst test volume data " * 200
        vol_path = volumes_dir / f"{volume_id}.vol"
        vol_path.write_bytes(volume_data)

        service = VolumeTieringService(
            volumes_dir=volumes_dir,
            cloud_transport=cloud,
            config=config,
        )

        # Tier
        await service.sweep_once()
        assert not vol_path.exists()
        cloud_key = f"volumes/{volume_id}.vol"

        # Burst reads to trigger cache promotion (background download)
        for i in range(3):
            result = service.read_range(cloud_key, offset=i * 10, size=10, volume_id=volume_id)
            assert result == volume_data[i * 10 : i * 10 + 10]

        # Wait for background download thread to complete
        import time as time_mod

        time_mod.sleep(2.0)  # S3 download takes longer than mock
        assert service.cache.has(volume_id)

        # Read from cache (no cloud call)
        cached_result = service.read_range(cloud_key, offset=50, size=20, volume_id=volume_id)
        assert cached_result == volume_data[50:70]

        # Rehydrate — download back to volumes dir
        assert service.rehydrate_volume(volume_id)
        assert vol_path.exists()
        assert vol_path.read_bytes() == volume_data
        assert service.manifest.get(volume_id) is None  # state cleared

        # Cleanup S3
        try:
            cloud.remove(cloud_key)
        except Exception:
            pass

    def test_latency_under_200ms(self, tmp_path):
        """Acceptance criterion: tiered read latency < 200ms."""
        import time as time_mod

        from nexus.backends.transports.s3_transport import S3Transport

        cloud = S3Transport(bucket_name=TEST_BUCKET)

        # Upload a test object
        key = f"volumes/{TEST_PREFIX}/latency_test.dat"
        data = b"X" * 4096
        cloud.store(key, data)

        # Warm up connection
        cloud.get_blob_range(key, 0, 10)

        # Measure range read latency
        iterations = 10
        start = time_mod.perf_counter()
        for i in range(iterations):
            cloud.get_blob_range(key, offset=i * 100, size=96)
        elapsed = time_mod.perf_counter() - start

        avg_ms = (elapsed / iterations) * 1000

        # Cleanup
        try:
            cloud.remove(key)
        except Exception:
            pass

        assert avg_ms < 200.0, f"Average S3 range-read latency {avg_ms:.1f}ms exceeds 200ms"


class TestCASLocalBackendTieringWiring:
    """Test CASLocalBackend with tiering_config wiring."""

    def test_tiering_service_created_when_enabled(self, tmp_path):
        from nexus.backends.storage.cas_local import CASLocalBackend
        from nexus.core.config import TieringConfig

        config = TieringConfig(
            enabled=True,
            cloud_backend="s3",
            cloud_bucket=TEST_BUCKET,
        )

        backend = CASLocalBackend(
            root_path=tmp_path,
            use_volume_packing=True,
            tiering_config=config,
        )

        assert backend._tiering_service is not None
        assert hasattr(backend._transport, "tiering")
        assert backend._transport.tiering is not None

    def test_tiering_not_created_when_disabled(self, tmp_path):
        from nexus.backends.storage.cas_local import CASLocalBackend
        from nexus.core.config import TieringConfig

        config = TieringConfig(enabled=False)

        backend = CASLocalBackend(
            root_path=tmp_path,
            use_volume_packing=True,
            tiering_config=config,
        )

        assert backend._tiering_service is None

    def test_tiering_not_created_without_volume_packing(self, tmp_path):
        from nexus.backends.storage.cas_local import CASLocalBackend
        from nexus.core.config import TieringConfig

        config = TieringConfig(
            enabled=True,
            cloud_backend="s3",
            cloud_bucket=TEST_BUCKET,
        )

        backend = CASLocalBackend(
            root_path=tmp_path,
            use_volume_packing=False,
            tiering_config=config,
        )

        # Tiering requires BlobPackLocalTransport, not LocalTransport
        assert backend._tiering_service is None
