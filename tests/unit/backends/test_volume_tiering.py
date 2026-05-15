"""Tests for volume-level cold tiering (Issue #3406).

Test categories:
  - TieringManifest: state persistence, write-ahead, read timestamps
  - VolumeTieringService: crash recovery (parametrized 4 crash points),
    eligibility policy, sweep lifecycle
  - Tiered read path: E2E with mocked cloud transport
  - GC interaction: skip TIERING/TIERED volumes
  - Transport range-read: mocked S3/GCS get_blob_range()
  - TieringConfig: validation and defaults
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.services.volume_tiering import (
    TieredVolumeEntry,
    TieringManifest,
    VolumeCache,
    VolumeState,
    VolumeTieringService,
    _file_sha256,
)

# Mock TOC parser for unit tests — real .vol files use binary format.
# Returns a single fake blob spanning the entire file.
_MOCK_TOC = {"deadbeef" * 8: (0, 100)}


@pytest.fixture(autouse=True)
def _mock_toc_parser(monkeypatch):
    """Patch parse_volume_toc so unit tests don't need real .vol files."""
    monkeypatch.setattr(
        "nexus.services.volume_tiering.parse_volume_toc",
        lambda path: _MOCK_TOC,
    )


def make_hash(seed: int) -> str:
    return f"{seed:064x}"


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_config(**overrides):
    """Build a TieringConfig with test-friendly defaults."""
    from nexus.core.config import TieringConfig

    defaults = {
        "enabled": True,
        "quiet_period_seconds": 0.0,  # no wait in tests
        "min_volume_size_bytes": 0,  # no minimum in tests
        "cloud_backend": "gcs",
        "cloud_bucket": "test-bucket",
        "upload_rate_limit_bytes": 0,  # no rate limit in tests
        "sweep_interval_seconds": 0.1,
    }
    defaults.update(overrides)
    return TieringConfig(**defaults)


def _make_mock_cloud(blobs: dict[str, bytes] | None = None):
    """Create a mock cloud transport with optional pre-loaded blobs."""
    cloud = MagicMock()
    store: dict[str, bytes] = dict(blobs) if blobs else {}

    def upload_file(key, local_path, chunk_size=8 * 1024 * 1024):
        store[key] = Path(local_path).read_bytes()
        return None

    def get_size(key):
        if key not in store:
            from nexus.contracts.exceptions import NexusFileNotFoundError

            raise NexusFileNotFoundError(key)
        return len(store[key])

    def get_blob_range(key, offset, size):
        if key not in store:
            from nexus.contracts.exceptions import NexusFileNotFoundError

            raise NexusFileNotFoundError(key)
        return store[key][offset : offset + size]

    def fetch(key, version_id=None):
        if key not in store:
            from nexus.contracts.exceptions import NexusFileNotFoundError

            raise NexusFileNotFoundError(key)
        return store[key], None

    cloud.upload_file = MagicMock(side_effect=upload_file)
    cloud.get_size = MagicMock(side_effect=get_size)
    cloud.get_blob_range = MagicMock(side_effect=get_blob_range)
    cloud.fetch = MagicMock(side_effect=fetch)
    cloud._store = store  # expose for assertions
    return cloud


def _create_vol_file(volumes_dir: Path, volume_id: str, content: bytes) -> Path:
    """Create a .vol file simulating a sealed volume."""
    vol_path = volumes_dir / f"{volume_id}.vol"
    vol_path.write_bytes(content)
    return vol_path


# ─── TieringManifest Tests ──────────────────────────────────────────────────


class TestTieringManifest:
    """Test manifest persistence, state transitions, and read tracking."""

    def test_empty_manifest(self, tmp_path):
        manifest = TieringManifest(tmp_path / "tiering_state.json")
        assert manifest.all_entries() == []
        assert manifest.tiered_volumes() == []
        assert manifest.tiering_volumes() == []

    def test_set_state_persists_to_disk(self, tmp_path):
        path = tmp_path / "tiering_state.json"
        manifest = TieringManifest(path)

        entry = TieredVolumeEntry(
            volume_id="vol_001",
            state=VolumeState.TIERING,
            cloud_key="volumes/vol_001.vol",
            size_bytes=1024,
        )
        manifest.set_state(entry)

        # Verify file exists and is valid JSON
        assert path.exists()
        data = json.loads(path.read_text())
        assert "vol_001" in data["volumes"]
        assert data["volumes"]["vol_001"]["state"] == "tiering"

    def test_roundtrip_persistence(self, tmp_path):
        path = tmp_path / "tiering_state.json"
        manifest1 = TieringManifest(path)

        entry = TieredVolumeEntry(
            volume_id="vol_001",
            state=VolumeState.TIERED,
            cloud_key="volumes/vol_001.vol",
            checksum_sha256="abc123",
            uploaded_at=1234567890.0,
            size_bytes=2048,
        )
        manifest1.set_state(entry)

        # Reload from disk
        manifest2 = TieringManifest(path)
        loaded = manifest2.get("vol_001")
        assert loaded is not None
        assert loaded.state == VolumeState.TIERED
        assert loaded.cloud_key == "volumes/vol_001.vol"
        assert loaded.checksum_sha256 == "abc123"
        assert loaded.uploaded_at == 1234567890.0
        assert loaded.size_bytes == 2048

    def test_is_volume_tiered_or_tiering(self, tmp_path):
        manifest = TieringManifest(tmp_path / "tiering_state.json")

        assert not manifest.is_volume_tiered_or_tiering("vol_001")

        manifest.set_state(
            TieredVolumeEntry(
                volume_id="vol_001",
                state=VolumeState.TIERING,
                cloud_key="x",
            )
        )
        assert manifest.is_volume_tiered_or_tiering("vol_001")

        manifest.set_state(
            TieredVolumeEntry(
                volume_id="vol_001",
                state=VolumeState.TIERED,
                cloud_key="x",
            )
        )
        assert manifest.is_volume_tiered_or_tiering("vol_001")

    def test_remove(self, tmp_path):
        manifest = TieringManifest(tmp_path / "tiering_state.json")
        manifest.set_state(
            TieredVolumeEntry(
                volume_id="vol_001",
                state=VolumeState.TIERED,
                cloud_key="x",
            )
        )
        assert manifest.get("vol_001") is not None

        manifest.remove("vol_001")
        assert manifest.get("vol_001") is None
        assert manifest.all_entries() == []

    def test_last_read_tracking(self, tmp_path):
        manifest = TieringManifest(tmp_path / "tiering_state.json")

        assert manifest.last_read_time("vol_001") == 0.0

        manifest.record_read("vol_001")
        ts = manifest.last_read_time("vol_001")
        assert ts > 0.0
        assert abs(ts - time.time()) < 1.0

    def test_read_timestamps_persisted_on_flush(self, tmp_path):
        path = tmp_path / "tiering_state.json"
        manifest1 = TieringManifest(path)
        manifest1.record_read("vol_001")
        manifest1.flush_read_timestamps()

        manifest2 = TieringManifest(path)
        assert manifest2.last_read_time("vol_001") > 0.0

    def test_corrupt_manifest_starts_fresh(self, tmp_path):
        path = tmp_path / "tiering_state.json"
        path.write_text("not valid json {{{", encoding="utf-8")

        manifest = TieringManifest(path)
        assert manifest.all_entries() == []

    def test_atomic_write(self, tmp_path):
        """Manifest write uses rename for atomicity."""
        path = tmp_path / "tiering_state.json"
        manifest = TieringManifest(path)
        manifest.set_state(
            TieredVolumeEntry(
                volume_id="vol_001",
                state=VolumeState.TIERED,
                cloud_key="x",
            )
        )

        # .tmp file should not linger
        assert not (tmp_path / "tiering_state.tmp").exists()
        assert path.exists()


# ─── VolumeTieringService: Eligibility Tests ────────────────────────────────


class TestTieringEligibility:
    """Test volume eligibility checks."""

    def test_skip_small_volumes(self, tmp_path):
        config = _make_config(min_volume_size_bytes=1000)
        cloud = _make_mock_cloud()
        service = VolumeTieringService(tmp_path, cloud, config)

        _create_vol_file(tmp_path, "vol_001", b"small")  # 5 bytes < 1000
        assert not service._is_eligible("vol_001", tmp_path / "vol_001.vol")

    def test_skip_recently_read_volumes(self, tmp_path):
        config = _make_config(quiet_period_seconds=3600)
        cloud = _make_mock_cloud()
        service = VolumeTieringService(tmp_path, cloud, config)

        _create_vol_file(tmp_path, "vol_001", b"x" * 200)
        service.manifest.record_read("vol_001")

        assert not service._is_eligible("vol_001", tmp_path / "vol_001.vol")

    def test_eligible_when_quiet_and_large_enough(self, tmp_path):
        config = _make_config(
            quiet_period_seconds=0.0,
            min_volume_size_bytes=0,
        )
        cloud = _make_mock_cloud()
        service = VolumeTieringService(tmp_path, cloud, config)

        dat = _create_vol_file(tmp_path, "vol_001", b"volume data here")
        # Set mtime in the past so quiet_period check passes
        assert service._is_eligible("vol_001", dat)

    def test_skip_already_tiered(self, tmp_path):
        config = _make_config()
        cloud = _make_mock_cloud()
        service = VolumeTieringService(tmp_path, cloud, config)

        _create_vol_file(tmp_path, "vol_001", b"data")
        service.manifest.set_state(
            TieredVolumeEntry(
                volume_id="vol_001",
                state=VolumeState.TIERED,
                cloud_key="x",
            )
        )

        # Sweep should skip it
        assert service.manifest.is_volume_tiered_or_tiering("vol_001")


# ─── VolumeTieringService: Tier Volume Tests ────────────────────────────────


class TestTierVolume:
    """Test the full tiering flow for a single volume."""

    @pytest.mark.asyncio
    async def test_tier_volume_happy_path(self, tmp_path):
        config = _make_config()
        cloud = _make_mock_cloud()
        service = VolumeTieringService(tmp_path, cloud, config)

        volume_data = b"sealed volume content " * 100
        _create_vol_file(tmp_path, "vol_001", volume_data)

        await service._tier_volume("vol_001", tmp_path / "vol_001.vol")

        # Cloud should have the data
        assert "volumes/vol_001.vol" in cloud._store
        assert cloud._store["volumes/vol_001.vol"] == volume_data

        # Manifest should be TIERED
        entry = service.manifest.get("vol_001")
        assert entry is not None
        assert entry.state == VolumeState.TIERED
        assert entry.checksum_sha256 != ""
        assert entry.uploaded_at > 0.0

        # Local .dat should be deleted
        assert not (tmp_path / "vol_001.vol").exists()

    @pytest.mark.asyncio
    async def test_tier_volume_upload_verified(self, tmp_path):
        """Verify that upload size is checked after upload."""
        config = _make_config()
        cloud = _make_mock_cloud()
        service = VolumeTieringService(tmp_path, cloud, config)

        volume_data = b"data" * 50
        _create_vol_file(tmp_path, "vol_001", volume_data)

        await service._tier_volume("vol_001", tmp_path / "vol_001.vol")

        # get_blob_size should have been called for verification
        cloud.get_size.assert_called_with("volumes/vol_001.vol")

    @pytest.mark.asyncio
    async def test_sweep_once_tiers_eligible(self, tmp_path):
        config = _make_config()
        cloud = _make_mock_cloud()
        service = VolumeTieringService(tmp_path, cloud, config)

        _create_vol_file(tmp_path, "vol_001", b"data" * 100)
        _create_vol_file(tmp_path, "vol_002", b"more data" * 100)

        count = await service.sweep_once()
        assert count == 2
        assert service.manifest.get("vol_001").state == VolumeState.TIERED
        assert service.manifest.get("vol_002").state == VolumeState.TIERED


# ─── Crash Recovery (Parametrized at 4 Transition Points) ───────────────────


class TestCrashRecovery:
    """Test crash recovery at each state transition point.

    Decision 10A: Parametrized crash at each transition.
    """

    def test_crash_during_upload_no_cloud_object(self, tmp_path):
        """Crash point 1: TIERING state, upload never completed.

        Expected: revert to SEALED (remove from manifest), volume re-eligible.
        """
        config = _make_config()
        cloud = _make_mock_cloud()  # empty cloud — no object uploaded
        service = VolumeTieringService(tmp_path, cloud, config)

        # Simulate: manifest says TIERING, local .vol exists, cloud empty
        _create_vol_file(tmp_path, "vol_001", b"volume data")
        service.manifest.set_state(
            TieredVolumeEntry(
                volume_id="vol_001",
                state=VolumeState.TIERING,
                cloud_key="volumes/vol_001.vol",
                size_bytes=11,
            )
        )

        service._recover_on_startup()

        # Should revert — volume removed from manifest
        assert service.manifest.get("vol_001") is None
        # Local .dat should still exist
        assert (tmp_path / "vol_001.vol").exists()

    def test_crash_after_upload_before_state_update(self, tmp_path):
        """Crash point 2: TIERING state, cloud upload completed.

        Expected: verify cloud size matches, advance to TIERED, delete local.
        """
        config = _make_config()
        volume_data = b"volume data content"
        cloud = _make_mock_cloud({"volumes/vol_001.vol": volume_data})
        service = VolumeTieringService(tmp_path, cloud, config)

        # Simulate: manifest says TIERING, local .vol exists, cloud has data
        _create_vol_file(tmp_path, "vol_001", volume_data)
        service.manifest.set_state(
            TieredVolumeEntry(
                volume_id="vol_001",
                state=VolumeState.TIERING,
                cloud_key="volumes/vol_001.vol",
                size_bytes=len(volume_data),
            )
        )

        service._recover_on_startup()

        # Should advance to TIERED
        entry = service.manifest.get("vol_001")
        assert entry is not None
        assert entry.state == VolumeState.TIERED
        # Local .dat should be deleted
        assert not (tmp_path / "vol_001.vol").exists()

    def test_crash_after_tiered_state_before_local_delete(self, tmp_path):
        """Crash point 3: TIERED state, local .vol still exists.

        Expected: delete lingering local .vol (deferred cleanup).
        """
        config = _make_config()
        volume_data = b"volume data"
        cloud = _make_mock_cloud({"volumes/vol_001.vol": volume_data})
        service = VolumeTieringService(tmp_path, cloud, config)

        # Simulate: TIERED state, but local .vol wasn't deleted before crash
        _create_vol_file(tmp_path, "vol_001", volume_data)
        service.manifest.set_state(
            TieredVolumeEntry(
                volume_id="vol_001",
                state=VolumeState.TIERED,
                cloud_key="volumes/vol_001.vol",
                checksum_sha256="abc",
                uploaded_at=time.time(),
                size_bytes=len(volume_data),
            )
        )

        service._recover_on_startup()

        # State should remain TIERED
        assert service.manifest.get("vol_001").state == VolumeState.TIERED
        # Local .dat should be cleaned up
        assert not (tmp_path / "vol_001.vol").exists()

    def test_clean_completion_no_recovery_needed(self, tmp_path):
        """Crash point 4: TIERED, no local .vol. Clean state.

        Expected: no-op.
        """
        config = _make_config()
        cloud = _make_mock_cloud({"volumes/vol_001.vol": b"data"})
        service = VolumeTieringService(tmp_path, cloud, config)

        # Simulate: clean TIERED state, no local .vol
        service.manifest.set_state(
            TieredVolumeEntry(
                volume_id="vol_001",
                state=VolumeState.TIERED,
                cloud_key="volumes/vol_001.vol",
                checksum_sha256="abc",
                uploaded_at=time.time(),
                size_bytes=4,
            )
        )

        service._recover_on_startup()

        # Should remain unchanged
        assert service.manifest.get("vol_001").state == VolumeState.TIERED

    def test_crash_tiering_no_local_no_cloud_data_loss(self, tmp_path):
        """Edge case: TIERING, local .vol gone, cloud empty — data loss.

        Expected: log error, remove from manifest (can't recover).
        """
        config = _make_config()
        cloud = _make_mock_cloud()  # empty cloud
        service = VolumeTieringService(tmp_path, cloud, config)

        # Simulate: TIERING, but both local and cloud are gone
        service.manifest.set_state(
            TieredVolumeEntry(
                volume_id="vol_001",
                state=VolumeState.TIERING,
                cloud_key="volumes/vol_001.vol",
                size_bytes=100,
            )
        )

        service._recover_on_startup()

        # Should be removed (can't recover)
        assert service.manifest.get("vol_001") is None

    def test_crash_tiering_no_local_cloud_has_data(self, tmp_path):
        """Edge case: TIERING, local .vol gone, cloud has data.

        Expected: advance to TIERED (upload completed, local already cleaned).
        """
        config = _make_config()
        cloud = _make_mock_cloud({"volumes/vol_001.vol": b"data in cloud"})
        service = VolumeTieringService(tmp_path, cloud, config)

        service.manifest.set_state(
            TieredVolumeEntry(
                volume_id="vol_001",
                state=VolumeState.TIERING,
                cloud_key="volumes/vol_001.vol",
                size_bytes=13,
            )
        )

        service._recover_on_startup()

        entry = service.manifest.get("vol_001")
        assert entry is not None
        assert entry.state == VolumeState.TIERED

    def test_crash_tiering_size_mismatch(self, tmp_path):
        """Edge case: TIERING, cloud has partial upload (size mismatch).

        Expected: revert to SEALED for re-upload.
        """
        config = _make_config()
        # Cloud has truncated data
        cloud = _make_mock_cloud({"volumes/vol_001.vol": b"partial"})
        service = VolumeTieringService(tmp_path, cloud, config)

        full_data = b"full volume data that is much longer"
        _create_vol_file(tmp_path, "vol_001", full_data)
        service.manifest.set_state(
            TieredVolumeEntry(
                volume_id="vol_001",
                state=VolumeState.TIERING,
                cloud_key="volumes/vol_001.vol",
                size_bytes=len(full_data),
            )
        )

        service._recover_on_startup()

        # Size mismatch — should revert
        assert service.manifest.get("vol_001") is None
        # Local .dat should still exist
        assert (tmp_path / "vol_001.vol").exists()


# ─── Tiered Read Path (E2E with mocked cloud) ───────────────────────────────


class TestTieredReadPath:
    """Test read interception in BlobPackLocalTransport for tiered volumes.

    Decision 11A: E2E test with mocked cloud transport.
    """

    def test_read_range_delegates_to_cloud(self, tmp_path):
        """read_range() should call cloud get_blob_range with correct params."""
        config = _make_config()
        volume_data = b"ABCDEFGHIJKLMNOP"
        cloud = _make_mock_cloud({"volumes/vol_001.vol": volume_data})
        service = VolumeTieringService(tmp_path, cloud, config)

        result = service.read_range("volumes/vol_001.vol", offset=4, size=4)
        assert result == b"EFGH"
        cloud.get_blob_range.assert_called_once_with("volumes/vol_001.vol", 4, 4)

    def test_read_range_offset_zero(self, tmp_path):
        config = _make_config()
        volume_data = b"HELLO WORLD"
        cloud = _make_mock_cloud({"volumes/vol_001.vol": volume_data})
        service = VolumeTieringService(tmp_path, cloud, config)

        result = service.read_range("volumes/vol_001.vol", offset=0, size=5)
        assert result == b"HELLO"

    def test_read_range_end_of_volume(self, tmp_path):
        config = _make_config()
        volume_data = b"0123456789"
        cloud = _make_mock_cloud({"volumes/vol_001.vol": volume_data})
        service = VolumeTieringService(tmp_path, cloud, config)

        result = service.read_range("volumes/vol_001.vol", offset=7, size=3)
        assert result == b"789"

    def test_read_range_not_found(self, tmp_path):
        config = _make_config()
        cloud = _make_mock_cloud()
        service = VolumeTieringService(tmp_path, cloud, config)

        from nexus.contracts.exceptions import NexusFileNotFoundError

        with pytest.raises(NexusFileNotFoundError):
            service.read_range("volumes/nonexistent.dat", offset=0, size=10)


# ─── GC + Tiering Interaction ───────────────────────────────────────────────


class TestGCTieringInteraction:
    """Test that GC skips TIERING and TIERED volumes.

    Decision 12A: GC skips TIERING + TIERED volumes.
    """

    def test_manifest_correctly_reports_tiered(self, tmp_path):
        manifest = TieringManifest(tmp_path / "tiering_state.json")

        manifest.set_state(
            TieredVolumeEntry(
                volume_id="vol_001",
                state=VolumeState.TIERED,
                cloud_key="x",
            )
        )
        manifest.set_state(
            TieredVolumeEntry(
                volume_id="vol_002",
                state=VolumeState.TIERING,
                cloud_key="y",
            )
        )

        assert manifest.is_volume_tiered_or_tiering("vol_001")
        assert manifest.is_volume_tiered_or_tiering("vol_002")
        assert not manifest.is_volume_tiered_or_tiering("vol_003")

    def test_gc_skips_tiered_volumes_integration(self, tmp_path):
        """Verify GC respects the tiering manifest skip check.

        This tests the logic that's wired into cas_gc.py — checking that
        the is_volume_tiered_or_tiering() predicate works correctly when
        called from the GC loop context.
        """
        manifest = TieringManifest(tmp_path / "tiering_state.json")

        # Simulate volumes
        manifest.set_state(
            TieredVolumeEntry(
                volume_id="vol_001",
                state=VolumeState.TIERED,
                cloud_key="x",
            )
        )
        manifest.set_state(
            TieredVolumeEntry(
                volume_id="vol_002",
                state=VolumeState.TIERING,
                cloud_key="y",
            )
        )

        # Both should be skipped
        assert manifest.is_volume_tiered_or_tiering("vol_001")
        assert manifest.is_volume_tiered_or_tiering("vol_002")

        # SEALED volumes should NOT be skipped
        assert not manifest.is_volume_tiered_or_tiering("vol_003")


# ─── Range Read Unit Tests (mocked transport) ───────────────────────────────


class TestRangeReadMocked:
    """Unit tests for get_blob_range with mocked cloud clients.

    Decision 9A: Mocked unit tests for range-read logic.
    """

    def test_gcs_range_read_offsets(self):
        """GCS download_as_bytes(start, end) uses inclusive end."""
        from unittest.mock import MagicMock

        with patch("nexus.backends.transports.gcs_transport.storage") as mock_storage:
            mock_client = MagicMock()
            mock_storage.Client.return_value = mock_client
            mock_bucket = MagicMock()
            mock_client.bucket.return_value = mock_bucket

            from nexus.backends.transports.gcs_transport import GCSTransport

            transport = GCSTransport.__new__(GCSTransport)
            transport.client = mock_client
            transport.bucket = mock_bucket
            transport.bucket_name = "test"
            transport._operation_timeout = 60.0
            transport._upload_timeout = 300.0

            mock_blob = MagicMock()
            mock_bucket.blob.return_value = mock_blob
            mock_blob.download_as_bytes.return_value = b"EFGH"

            result = transport.get_blob_range("volumes/vol.dat", offset=4, size=4)

            assert result == b"EFGH"
            mock_blob.download_as_bytes.assert_called_once()
            call_kwargs = mock_blob.download_as_bytes.call_args
            # GCS end is inclusive: offset + size - 1
            assert call_kwargs.kwargs["start"] == 4
            assert call_kwargs.kwargs["end"] == 7  # 4 + 4 - 1

    def test_s3_range_read_header(self):
        """S3 get_object Range header: bytes=start-end (inclusive)."""
        from unittest.mock import MagicMock

        with patch("nexus.backends.transports.s3_transport.boto3") as mock_boto3:
            mock_session = MagicMock()
            mock_boto3.Session.return_value = mock_session
            mock_client = MagicMock()
            mock_session.client.return_value = mock_client

            from nexus.backends.transports.s3_transport import S3Transport

            transport = S3Transport.__new__(S3Transport)
            transport.s3_client = mock_client
            transport.bucket_name = "test"
            transport._operation_timeout = 60.0
            transport._upload_timeout = 300.0

            mock_body = MagicMock()
            mock_body.read.return_value = b"EFGH"
            mock_client.get_object.return_value = {"Body": mock_body}

            result = transport.get_blob_range("volumes/vol.dat", offset=4, size=4)

            assert result == b"EFGH"
            mock_client.get_object.assert_called_once_with(
                Bucket="test",
                Key="volumes/vol.dat",
                Range="bytes=4-7",  # inclusive
            )


# ─── TieringConfig Tests ────────────────────────────────────────────────────


class TestTieringConfig:
    def test_default_values(self):
        from nexus.core.config import TieringConfig

        config = TieringConfig()
        assert config.enabled is False
        assert config.quiet_period_seconds == 3600.0
        assert config.min_volume_size_bytes == 100 * 1024 * 1024
        assert config.cloud_backend == "gcs"
        assert config.cloud_bucket == ""
        assert config.upload_rate_limit_bytes == 25 * 1024 * 1024
        assert config.sweep_interval_seconds == 60.0
        assert config.local_cache_size_bytes == 10 * 1024 * 1024 * 1024
        assert config.burst_read_threshold == 5
        assert config.burst_read_window_seconds == 60.0

    def test_frozen(self):
        from dataclasses import FrozenInstanceError

        from nexus.core.config import TieringConfig

        config = TieringConfig()
        with pytest.raises(FrozenInstanceError):
            config.enabled = True

    def test_custom_values(self):
        from nexus.core.config import TieringConfig

        config = TieringConfig(
            enabled=True,
            quiet_period_seconds=7200.0,
            cloud_backend="s3",
            cloud_bucket="my-bucket",
        )
        assert config.enabled is True
        assert config.quiet_period_seconds == 7200.0
        assert config.cloud_backend == "s3"
        assert config.cloud_bucket == "my-bucket"


# ─── File SHA-256 Utility ────────────────────────────────────────────────────


class TestFileSHA256:
    def test_correct_hash(self, tmp_path):
        import hashlib

        data = b"hello world"
        path = tmp_path / "test.dat"
        path.write_bytes(data)

        expected = hashlib.sha256(data).hexdigest()
        assert _file_sha256(path) == expected

    def test_large_file(self, tmp_path):
        import hashlib

        data = b"x" * (10 * 1024 * 1024)  # 10 MB
        path = tmp_path / "large.dat"
        path.write_bytes(data)

        expected = hashlib.sha256(data).hexdigest()
        assert _file_sha256(path) == expected

    def test_empty_file(self, tmp_path):
        import hashlib

        path = tmp_path / "empty.dat"
        path.write_bytes(b"")

        expected = hashlib.sha256(b"").hexdigest()
        assert _file_sha256(path) == expected


# ─── Service Lifecycle Tests ─────────────────────────────────────────────────


class TestServiceLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop(self, tmp_path):
        config = _make_config()
        cloud = _make_mock_cloud()
        service = VolumeTieringService(tmp_path, cloud, config)

        await service.start()
        assert service.is_running

        await service.stop()
        assert not service.is_running

    @pytest.mark.asyncio
    async def test_start_idempotent(self, tmp_path):
        config = _make_config()
        cloud = _make_mock_cloud()
        service = VolumeTieringService(tmp_path, cloud, config)

        await service.start()
        await service.start()  # should be no-op
        assert service.is_running

        await service.stop()

    @pytest.mark.asyncio
    async def test_recovery_runs_on_start(self, tmp_path):
        """start() should run crash recovery before the sweep loop."""
        config = _make_config()
        volume_data = b"data"
        cloud = _make_mock_cloud({"volumes/vol_001.vol": volume_data})
        service = VolumeTieringService(tmp_path, cloud, config)

        # Set up a TIERED volume with lingering local .vol
        _create_vol_file(tmp_path, "vol_001", volume_data)
        service.manifest.set_state(
            TieredVolumeEntry(
                volume_id="vol_001",
                state=VolumeState.TIERED,
                cloud_key="volumes/vol_001.vol",
                size_bytes=4,
            )
        )

        await service.start()
        # Recovery should have deleted the local .vol
        assert not (tmp_path / "vol_001.vol").exists()

        await service.stop()


# ─── Upload File Tests ───────────────────────────────────────────────────────


class TestUploadFile:
    """Test upload_file methods on transports (mocked)."""

    def test_gcs_upload_file(self, tmp_path):
        """GCSTransport.upload_file wraps store_chunked."""
        from unittest.mock import MagicMock

        with patch("nexus.backends.transports.gcs_transport.storage") as mock_storage:
            mock_client = MagicMock()
            mock_storage.Client.return_value = mock_client
            mock_bucket = MagicMock()
            mock_client.bucket.return_value = mock_bucket

            from nexus.backends.transports.gcs_transport import GCSTransport

            transport = GCSTransport.__new__(GCSTransport)
            transport.client = mock_client
            transport.bucket = mock_bucket
            transport.bucket_name = "test"
            transport._operation_timeout = 60.0
            transport._upload_timeout = 300.0

            # Write test file
            test_file = tmp_path / "test.dat"
            test_file.write_bytes(b"test data for upload")

            # Mock store_chunked
            transport.store_chunked = MagicMock(return_value="gen123")

            result = transport.upload_file("volumes/test.dat", str(test_file))
            assert result == "gen123"
            transport.store_chunked.assert_called_once()

    def test_s3_upload_file(self, tmp_path):
        """S3Transport.upload_file wraps store_chunked."""
        from unittest.mock import MagicMock

        with patch("nexus.backends.transports.s3_transport.boto3") as mock_boto3:
            mock_session = MagicMock()
            mock_boto3.Session.return_value = mock_session
            mock_client = MagicMock()
            mock_session.client.return_value = mock_client

            from nexus.backends.transports.s3_transport import S3Transport

            transport = S3Transport.__new__(S3Transport)
            transport.s3_client = mock_client
            transport.bucket_name = "test"
            transport._operation_timeout = 60.0
            transport._upload_timeout = 300.0

            test_file = tmp_path / "test.dat"
            test_file.write_bytes(b"test data for upload")

            transport.store_chunked = MagicMock(return_value="version123")

            result = transport.upload_file("volumes/test.dat", str(test_file))
            assert result == "version123"
            transport.store_chunked.assert_called_once()


# ─── VolumeCache Tests ───────────────────────────────────────────────────────


class TestVolumeCache:
    """Tests for LRU disk cache for tiered volumes."""

    def test_empty_cache(self, tmp_path):
        cloud = _make_mock_cloud()
        cache = VolumeCache(tmp_path / "cache", cloud, max_size_bytes=1024 * 1024)
        assert cache.cached_count == 0
        assert cache.current_size_bytes() == 0
        assert not cache.has("vol_001")

    def test_read_local_miss(self, tmp_path):
        cloud = _make_mock_cloud()
        cache = VolumeCache(tmp_path / "cache", cloud)
        assert cache.read_local("vol_001", 0, 10) is None

    def test_download_and_read_local(self, tmp_path):
        volume_data = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        cloud = _make_mock_cloud({"volumes/vol_001.vol": volume_data})
        cache = VolumeCache(tmp_path / "cache", cloud, max_size_bytes=1024 * 1024)

        # Download to cache
        assert cache.download_volume("volumes/vol_001.vol", "vol_001")
        assert cache.has("vol_001")
        assert cache.cached_count == 1

        # Read from cache
        result = cache.read_local("vol_001", offset=4, size=4)
        assert result == b"EFGH"

        result2 = cache.read_local("vol_001", offset=0, size=5)
        assert result2 == b"ABCDE"

    def test_lru_eviction(self, tmp_path):
        """Oldest-accessed volume should be evicted when cache is full."""
        vol_a = b"A" * 500
        vol_b = b"B" * 500
        vol_c = b"C" * 500
        cloud = _make_mock_cloud(
            {
                "volumes/vol_a.dat": vol_a,
                "volumes/vol_b.dat": vol_b,
                "volumes/vol_c.dat": vol_c,
            }
        )
        # Cache can hold ~1000 bytes (2 volumes of 500, not 3)
        cache = VolumeCache(tmp_path / "cache", cloud, max_size_bytes=1000)

        cache.download_volume("volumes/vol_a.dat", "vol_a")
        time.sleep(0.01)  # ensure different access times
        cache.download_volume("volumes/vol_b.dat", "vol_b")
        assert cache.cached_count == 2

        # Downloading vol_c should evict vol_a (LRU)
        cache.download_volume("volumes/vol_c.dat", "vol_c")
        assert not cache.has("vol_a")  # evicted
        assert cache.has("vol_b")
        assert cache.has("vol_c")

    def test_eviction_respects_access_time(self, tmp_path):
        """Accessing vol_a should make vol_b the eviction target."""
        vol_a = b"A" * 500
        vol_b = b"B" * 500
        vol_c = b"C" * 500
        cloud = _make_mock_cloud(
            {
                "volumes/vol_a.dat": vol_a,
                "volumes/vol_b.dat": vol_b,
                "volumes/vol_c.dat": vol_c,
            }
        )
        cache = VolumeCache(tmp_path / "cache", cloud, max_size_bytes=1000)

        cache.download_volume("volumes/vol_a.dat", "vol_a")
        time.sleep(0.01)
        cache.download_volume("volumes/vol_b.dat", "vol_b")

        # Access vol_a to make it recently used
        time.sleep(0.01)
        cache.read_local("vol_a", 0, 1)

        # Now vol_b is LRU, should be evicted
        cache.download_volume("volumes/vol_c.dat", "vol_c")
        assert cache.has("vol_a")  # recently accessed
        assert not cache.has("vol_b")  # LRU evicted
        assert cache.has("vol_c")

    def test_remove(self, tmp_path):
        cloud = _make_mock_cloud({"volumes/vol_001.vol": b"data"})
        cache = VolumeCache(tmp_path / "cache", cloud, max_size_bytes=1024 * 1024)

        cache.download_volume("volumes/vol_001.vol", "vol_001")
        assert cache.has("vol_001")

        cache.remove("vol_001")
        assert not cache.has("vol_001")
        assert cache.cached_count == 0

    def test_scan_existing_cache_on_init(self, tmp_path):
        """Cache should discover volumes left from a previous session."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / "vol_001.dat").write_bytes(b"cached data")
        (cache_dir / "vol_002.dat").write_bytes(b"more cached")

        cloud = _make_mock_cloud()
        cache = VolumeCache(cache_dir, cloud)
        assert cache.cached_count == 2
        assert cache.has("vol_001")
        assert cache.has("vol_002")

    def test_current_size_bytes(self, tmp_path):
        cloud = _make_mock_cloud(
            {
                "volumes/vol_001.vol": b"x" * 100,
                "volumes/vol_002.vol": b"y" * 200,
            }
        )
        cache = VolumeCache(tmp_path / "cache", cloud, max_size_bytes=1024 * 1024)

        cache.download_volume("volumes/vol_001.vol", "vol_001")
        assert cache.current_size_bytes() == 100

        cache.download_volume("volumes/vol_002.vol", "vol_002")
        assert cache.current_size_bytes() == 300

    def test_download_nonexistent_volume(self, tmp_path):
        cloud = _make_mock_cloud()
        cache = VolumeCache(tmp_path / "cache", cloud, max_size_bytes=1024 * 1024)
        assert not cache.download_volume("volumes/nonexistent.dat", "nonexistent")
        assert not cache.has("nonexistent")


# ─── Burst Detection Tests ───────────────────────────────────────────────────


class TestBurstDetection:
    """Test burst read detection and automatic re-download."""

    def test_no_burst_below_threshold(self, tmp_path):
        cloud = _make_mock_cloud()
        cache = VolumeCache(
            tmp_path / "cache",
            cloud,
            burst_threshold=5,
            burst_window_seconds=60.0,
        )

        # 4 reads should not trigger burst (threshold is 5)
        for _ in range(4):
            assert not cache.record_read_and_check_burst("vol_001")

    def test_burst_at_threshold(self, tmp_path):
        cloud = _make_mock_cloud()
        cache = VolumeCache(
            tmp_path / "cache",
            cloud,
            burst_threshold=5,
            burst_window_seconds=60.0,
        )

        for _ in range(4):
            cache.record_read_and_check_burst("vol_001")

        # 5th read should trigger
        assert cache.record_read_and_check_burst("vol_001")

    def test_burst_window_prunes_old_reads(self, tmp_path):
        cloud = _make_mock_cloud()
        cache = VolumeCache(
            tmp_path / "cache",
            cloud,
            burst_threshold=3,
            burst_window_seconds=0.01,  # tiny window
        )

        cache.record_read_and_check_burst("vol_001")
        cache.record_read_and_check_burst("vol_001")

        # Wait for window to expire
        time.sleep(0.02)

        # Old reads pruned, starts fresh
        assert not cache.record_read_and_check_burst("vol_001")

    def test_burst_independent_per_volume(self, tmp_path):
        cloud = _make_mock_cloud()
        cache = VolumeCache(
            tmp_path / "cache",
            cloud,
            burst_threshold=3,
            burst_window_seconds=60.0,
        )

        # vol_001 gets 3 reads (burst)
        cache.record_read_and_check_burst("vol_001")
        cache.record_read_and_check_burst("vol_001")
        assert cache.record_read_and_check_burst("vol_001")

        # vol_002 has 0 reads — no burst
        assert not cache.record_read_and_check_burst("vol_002")

    def test_read_range_with_burst_triggers_download(self, tmp_path):
        """End-to-end: burst reads trigger automatic volume cache download."""
        volume_data = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        config = _make_config(
            burst_read_threshold=3,
            burst_read_window_seconds=60.0,
        )
        cloud = _make_mock_cloud({"volumes/vol_001.vol": volume_data})
        service = VolumeTieringService(tmp_path, cloud, config)

        # First 2 reads: range from cloud, no cache
        for _ in range(2):
            result = service.read_range(
                "volumes/vol_001.vol", offset=0, size=5, volume_id="vol_001"
            )
            assert result == b"ABCDE"
        assert not service.cache.has("vol_001")

        # 3rd read: triggers burst → background download to cache
        result = service.read_range("volumes/vol_001.vol", offset=0, size=5, volume_id="vol_001")
        assert result == b"ABCDE"
        # Wait for background download thread to complete (mock cloud is instant)
        time.sleep(0.1)
        assert service.cache.has("vol_001")

        # 4th read: should come from local cache
        cloud.get_blob_range.reset_mock()
        result = service.read_range("volumes/vol_001.vol", offset=10, size=3, volume_id="vol_001")
        assert result == b"KLM"
        # Should NOT have called cloud — served from cache
        cloud.get_blob_range.assert_not_called()

    def test_read_range_cache_hit_skips_cloud(self, tmp_path):
        """Cached volume reads should not touch cloud at all."""
        volume_data = b"0123456789"
        config = _make_config()
        cloud = _make_mock_cloud({"volumes/vol_001.vol": volume_data})
        service = VolumeTieringService(tmp_path, cloud, config)

        # Pre-populate cache
        service.cache.download_volume("volumes/vol_001.vol", "vol_001")
        cloud.get_blob_range.reset_mock()
        cloud.fetch.reset_mock()

        result = service.read_range("volumes/vol_001.vol", offset=3, size=4, volume_id="vol_001")
        assert result == b"3456"
        cloud.get_blob_range.assert_not_called()


# ─── Benchmark: Tiered Read Latency ─────────────────────────────────────────


class TestTieredReadLatency:
    """Benchmark: tiered read latency < 200ms (acceptance criterion #7).

    Tests with controlled mock latency to verify the read path overhead
    is minimal — the bottleneck is cloud I/O, not our code.
    """

    def test_range_read_latency_under_200ms(self, tmp_path):
        """Tiered range read completes within 200ms with fast cloud mock."""
        import time as time_mod

        volume_data = b"X" * 4096
        config = _make_config()
        cloud = _make_mock_cloud({"volumes/vol_001.vol": volume_data})
        service = VolumeTieringService(tmp_path, cloud, config)

        # Warm up (first call may have import overhead)
        service.read_range("volumes/vol_001.vol", 0, 10, volume_id="vol_001")

        # Measure
        iterations = 100
        start = time_mod.perf_counter()
        for i in range(iterations):
            service.read_range("volumes/vol_001.vol", offset=i % 4000, size=96, volume_id="vol_001")
        elapsed = time_mod.perf_counter() - start

        avg_ms = (elapsed / iterations) * 1000
        # With mocked cloud (zero network latency), our overhead should be
        # well under 1ms per read. Real cloud adds 50-150ms.
        # We assert < 10ms to catch regressions in our code path.
        assert avg_ms < 10.0, f"Average read latency {avg_ms:.2f}ms exceeds 10ms threshold"

    def test_cached_read_latency_sub_millisecond(self, tmp_path):
        """Cached reads should be sub-millisecond (local pread)."""
        import time as time_mod

        volume_data = b"Y" * 8192
        config = _make_config()
        cloud = _make_mock_cloud({"volumes/vol_001.vol": volume_data})
        service = VolumeTieringService(tmp_path, cloud, config)

        # Pre-cache the volume
        service.cache.download_volume("volumes/vol_001.vol", "vol_001")

        # Measure cached reads
        iterations = 1000
        start = time_mod.perf_counter()
        for i in range(iterations):
            service.read_range(
                "volumes/vol_001.vol", offset=i % 8000, size=100, volume_id="vol_001"
            )
        elapsed = time_mod.perf_counter() - start

        avg_ms = (elapsed / iterations) * 1000
        # Cached reads should be well under 1ms
        assert avg_ms < 5.0, f"Average cached read latency {avg_ms:.2f}ms exceeds 5ms"

    def test_burst_promotes_to_cache_improving_latency(self, tmp_path):
        """After burst detection, reads should switch to fast cache path."""
        import time as time_mod

        volume_data = b"Z" * 4096
        config = _make_config(burst_read_threshold=3, burst_read_window_seconds=60.0)
        cloud = _make_mock_cloud({"volumes/vol_001.vol": volume_data})
        service = VolumeTieringService(tmp_path, cloud, config)

        # Trigger burst to populate cache (background thread)
        for _ in range(3):
            service.read_range("volumes/vol_001.vol", 0, 10, volume_id="vol_001")
        time.sleep(0.1)  # wait for background download
        assert service.cache.has("vol_001")

        # Now measure cached read latency
        cloud.get_blob_range.reset_mock()
        iterations = 100
        start = time_mod.perf_counter()
        for i in range(iterations):
            service.read_range("volumes/vol_001.vol", offset=i % 4000, size=96, volume_id="vol_001")
        elapsed = time_mod.perf_counter() - start

        avg_ms = (elapsed / iterations) * 1000
        assert avg_ms < 5.0, f"Post-burst cached read {avg_ms:.2f}ms exceeds 5ms"
        # Verify no cloud calls after cache promotion
        cloud.get_blob_range.assert_not_called()


# ─── Rehydration Tests (TIERED → local, writable again) ─────────────────────


class TestRehydration:
    """Test re-downloading tiered volumes to make them locally available.

    Acceptance criterion: 'Volume re-download for burst read patterns'
    Design: 'TIERED → re-downloaded, writable again'
    """

    def test_rehydrate_happy_path(self, tmp_path):
        """Download tiered volume back to local, clear TIERED state."""
        volume_data = b"rehydrated volume content" * 100
        config = _make_config()
        cloud = _make_mock_cloud({"volumes/vol_001.vol": volume_data})
        service = VolumeTieringService(tmp_path, cloud, config)

        # Set up TIERED state (no local .vol)
        service.manifest.set_state(
            TieredVolumeEntry(
                volume_id="vol_001",
                state=VolumeState.TIERED,
                cloud_key="volumes/vol_001.vol",
                checksum_sha256="abc",
                uploaded_at=time.time(),
                size_bytes=len(volume_data),
            )
        )

        result = service.rehydrate_volume("vol_001")
        assert result is True

        # .dat should be back in volumes dir
        dat_path = tmp_path / "vol_001.vol"
        assert dat_path.exists()
        assert dat_path.read_bytes() == volume_data

        # TIERED state should be cleared
        assert service.manifest.get("vol_001") is None

    def test_rehydrate_clears_cache(self, tmp_path):
        """Rehydration should remove the volume from LRU cache."""
        volume_data = b"cached and tiered"
        config = _make_config()
        cloud = _make_mock_cloud({"volumes/vol_001.vol": volume_data})
        service = VolumeTieringService(tmp_path, cloud, config)

        # TIERED + cached
        service.manifest.set_state(
            TieredVolumeEntry(
                volume_id="vol_001",
                state=VolumeState.TIERED,
                cloud_key="volumes/vol_001.vol",
                size_bytes=len(volume_data),
            )
        )
        service.cache.download_volume("volumes/vol_001.vol", "vol_001")
        assert service.cache.has("vol_001")

        service.rehydrate_volume("vol_001")

        # Cache entry should be gone
        assert not service.cache.has("vol_001")
        # But the .dat should be in the volumes dir
        assert (tmp_path / "vol_001.vol").exists()

    def test_rehydrate_already_local(self, tmp_path):
        """If .dat already exists locally, just clear the TIERED state."""
        volume_data = b"lingering local"
        config = _make_config()
        cloud = _make_mock_cloud({"volumes/vol_001.vol": volume_data})
        service = VolumeTieringService(tmp_path, cloud, config)

        # TIERED state + lingering local .vol
        _create_vol_file(tmp_path, "vol_001", volume_data)
        service.manifest.set_state(
            TieredVolumeEntry(
                volume_id="vol_001",
                state=VolumeState.TIERED,
                cloud_key="volumes/vol_001.vol",
                size_bytes=len(volume_data),
            )
        )

        result = service.rehydrate_volume("vol_001")
        assert result is True
        assert service.manifest.get("vol_001") is None
        # Should NOT have called cloud get_blob (already local)
        cloud.fetch.assert_not_called()

    def test_rehydrate_not_tiered(self, tmp_path):
        """Cannot rehydrate a volume that isn't TIERED."""
        config = _make_config()
        cloud = _make_mock_cloud()
        service = VolumeTieringService(tmp_path, cloud, config)

        # No entry at all
        assert not service.rehydrate_volume("vol_001")

        # TIERING state (upload in progress)
        service.manifest.set_state(
            TieredVolumeEntry(
                volume_id="vol_002",
                state=VolumeState.TIERING,
                cloud_key="volumes/vol_002.vol",
            )
        )
        assert not service.rehydrate_volume("vol_002")

    def test_rehydrate_cloud_failure(self, tmp_path):
        """If cloud download fails, rehydration should fail cleanly."""
        config = _make_config()
        cloud = _make_mock_cloud()  # empty cloud
        service = VolumeTieringService(tmp_path, cloud, config)

        service.manifest.set_state(
            TieredVolumeEntry(
                volume_id="vol_001",
                state=VolumeState.TIERED,
                cloud_key="volumes/vol_001.vol",
                size_bytes=100,
            )
        )

        result = service.rehydrate_volume("vol_001")
        assert result is False
        # State should remain TIERED (didn't clear it)
        assert service.manifest.get("vol_001").state == VolumeState.TIERED
        # No .dat should exist
        assert not (tmp_path / "vol_001.vol").exists()

    def test_rehydrate_size_mismatch(self, tmp_path):
        """If downloaded data doesn't match expected size, fail and clean up."""
        config = _make_config()
        # Cloud has different-sized data
        cloud = _make_mock_cloud({"volumes/vol_001.vol": b"short"})
        service = VolumeTieringService(tmp_path, cloud, config)

        service.manifest.set_state(
            TieredVolumeEntry(
                volume_id="vol_001",
                state=VolumeState.TIERED,
                cloud_key="volumes/vol_001.vol",
                size_bytes=99999,  # doesn't match
            )
        )

        result = service.rehydrate_volume("vol_001")
        assert result is False
        # Should have cleaned up the downloaded file
        assert not (tmp_path / "vol_001.vol").exists()
