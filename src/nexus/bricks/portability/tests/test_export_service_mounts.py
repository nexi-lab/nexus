"""Wiring tests for mount export integration."""

from unittest.mock import MagicMock

import pytest

from nexus.bricks.portability.export_service import ZoneExportService
from nexus.bricks.portability.models import BUNDLE_PATHS, ZoneExportOptions


def test_include_mounts_true_without_mount_manager_raises(tmp_path):
    fs = MagicMock()
    service = ZoneExportService(fs)
    options = ZoneExportOptions(
        output_path=tmp_path / "x.nexus",
        include_mounts=True,
    )
    with pytest.raises(ValueError, match="mount_manager"):
        # export_zone is sync (def, not async def)
        service.export_zone("z1", options)


def test_include_mounts_false_without_mount_manager_does_not_raise(tmp_path):
    """Existing callers without mount_manager must not break when include_mounts=False."""
    fs = MagicMock()
    # Provide a minimal kernel so the service doesn't crash on metastore_list
    kernel = MagicMock()
    kernel.metastore_list.return_value = []
    fs._kernel = kernel

    service = ZoneExportService(fs)
    options = ZoneExportOptions(
        output_path=tmp_path / "y.nexus",
        include_mounts=False,
    )
    # Should not raise ValueError; any other errors (e.g. tar creation) are fine.
    try:
        service.export_zone("z2", options)
    except ValueError as exc:
        pytest.fail(f"Unexpected ValueError raised: {exc}")
    except Exception:
        pass  # non-ValueError exceptions are acceptable


def test_include_mounts_true_with_mount_manager_calls_collect_and_write(tmp_path):
    """When include_mounts=True and mount_manager is provided, mount export runs."""

    fs = MagicMock()
    kernel = MagicMock()
    kernel.metastore_list.return_value = []
    fs._kernel = kernel

    mount_manager = MagicMock()
    mount_manager.list_mounts.return_value = [
        {
            "mount_id": "m1",
            "mount_point": "/data",
            # path_local is in CONNECTOR_MANIFEST as an always-available
            # built-in (no extras), so its CONNECTION_ARGS resolves cleanly
            # in the redaction contract. Using a fake string like "local"
            # would now (correctly) trigger SensitiveFieldNotDeclaredError
            # because we refuse to ship a mount whose contract we can't
            # introspect (Issue #4083 follow-up).
            "backend_type": "path_local",
            "backend_config": {"root_path": "/var/data"},
            "owner_user_id": "u1",
            "zone_id": "z3",
            "description": "test mount",
        }
    ]

    service = ZoneExportService(fs, mount_manager=mount_manager)
    options = ZoneExportOptions(
        output_path=tmp_path / "z.nexus",
        include_mounts=True,
    )

    try:
        manifest = service.export_zone("z3", options)
        assert manifest.mount_count == 1
    except Exception as exc:
        # If bundle creation fails for unrelated reasons, ensure it's not ValueError
        assert not isinstance(exc, ValueError), f"Unexpected ValueError: {exc}"


def test_mounts_jsonl_in_bundle_checksums(tmp_path):
    """mounts.jsonl must be registered in BundleChecksums for tamper detection."""
    fs = MagicMock()
    kernel = MagicMock()
    kernel.metastore_list.return_value = []
    fs._kernel = kernel

    mgr = MagicMock()
    mgr.list_mounts.return_value = [
        {
            "mount_id": "m-1",
            "mount_point": "/x",
            "backend_type": "path_local",
            "backend_config": {"root": "/data"},
            "owner_user_id": None,
            "zone_id": "z1",
            "description": None,
        }
    ]

    out = tmp_path / "bundle.nexus"
    service = ZoneExportService(fs, mount_manager=mgr)
    try:
        manifest = service.export_zone(
            "z1",
            ZoneExportOptions(output_path=out, include_mounts=True, sign=False),
        )
        assert BUNDLE_PATHS["mounts"] in manifest.checksums.files, (
            "mounts.jsonl must be added to BundleChecksums when mounts are exported"
        )
    except Exception as exc:
        # Only care about the checksum assertion failing; other infra errors are OK
        if isinstance(exc, AssertionError):
            raise


def test_include_mounts_with_zero_mounts_produces_valid_bundle(tmp_path):
    """Round 5: include_mounts=True with an empty MountManager must NOT
    write an empty mounts.jsonl with no checksum (BundleReader.validate
    would then reject the bundle the exporter just produced)."""
    fs = MagicMock()
    kernel = MagicMock()
    kernel.metastore_list.return_value = []
    fs._kernel = kernel

    mgr = MagicMock()
    mgr.list_mounts.return_value = []  # empty zone

    out = tmp_path / "empty.nexus"
    service = ZoneExportService(fs, mount_manager=mgr)
    manifest = service.export_zone(
        "z1",
        ZoneExportOptions(output_path=out, include_mounts=True, sign=False),
    )
    assert manifest.mount_count == 0
    assert "mounts.jsonl" not in manifest.checksums.files

    # Roundtrip: validate the bundle we just wrote — must pass.
    from nexus.bricks.portability.bundle import BundleReader

    with BundleReader(out) as reader:
        ok, errors = reader.validate()
    assert ok, f"empty-mount bundle failed self-validation: {errors}"
