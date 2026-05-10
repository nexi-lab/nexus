"""Tests for new mount-portability options."""

from pathlib import Path

from nexus.bricks.portability.models import ZoneExportOptions, ZoneImportOptions


def test_export_options_default_include_mounts_false():
    o = ZoneExportOptions(output_path=Path("/tmp/x.nexus"))
    assert o.include_mounts is False


def test_export_options_accepts_include_mounts_true():
    o = ZoneExportOptions(output_path=Path("/tmp/x.nexus"), include_mounts=True)
    assert o.include_mounts is True


def test_import_options_default_mount_overrides_none():
    o = ZoneImportOptions(bundle_path=Path("/tmp/x.nexus"))
    assert o.mount_overrides is None


def test_import_options_default_restore_mounts_true():
    o = ZoneImportOptions(bundle_path=Path("/tmp/x.nexus"))
    assert o.restore_mounts is True


def test_import_options_accepts_mount_overrides():
    overrides = {"m-1": {"access_key_id": "AKIA"}}
    o = ZoneImportOptions(bundle_path=Path("/tmp/x.nexus"), mount_overrides=overrides)
    assert o.mount_overrides == overrides


def test_import_options_accepts_restore_mounts_false():
    o = ZoneImportOptions(bundle_path=Path("/tmp/x.nexus"), restore_mounts=False)
    assert o.restore_mounts is False
