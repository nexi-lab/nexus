"""Wiring tests for mount import integration."""

import asyncio
import json
import tarfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus.bricks.portability.import_service import ZoneImportService
from nexus.bricks.portability.models import (
    BUNDLE_FORMAT_VERSION,
    MissingCredentialsError,
    ZoneImportOptions,
)


def _build_bundle_with_mount(tmp_path: Path) -> Path:
    """Create a minimal v3 bundle containing mounts.jsonl."""
    bundle_dir = tmp_path / "src"
    bundle_dir.mkdir()
    manifest = {
        "$schema": "https://nexus.io/schemas/manifest-v3.json",
        "format_version": BUNDLE_FORMAT_VERSION,
        "bundle_id": "550e8400-e29b-41d4-a716-446655440000",
        "source_zone_id": "z1",
        "export_timestamp": "2026-01-01T00:00:00+00:00",
        "statistics": {
            "file_count": 0,
            "total_size_bytes": 0,
            "content_blob_count": 0,
            "permission_count": 0,
            "embedding_count": 0,
            "mount_count": 1,
        },
        "options": {"include_content": True, "include_permissions": True},
        "checksums": {"algorithm": "sha256", "files": {}},
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest))
    (bundle_dir / "mounts.jsonl").write_text(
        json.dumps(
            {
                "mount_id": "m-1",
                "mount_point": "/x",
                "backend_type": "path_s3",
                "backend_config": {
                    "bucket_name": "acme",
                    "access_key_id": "${MOUNT_m-1_ACCESS_KEY_ID}",
                    "secret_access_key": "${MOUNT_m-1_SECRET_ACCESS_KEY}",
                },
                "owner_user_id": "alice",
                "zone_id": "z1",
                "description": None,
            }
        )
        + "\n"
    )

    out = tmp_path / "bundle.nexus"
    with tarfile.open(out, "w:gz") as tar:
        for p in bundle_dir.rglob("*"):
            tar.add(p, arcname=p.relative_to(bundle_dir))
    return out


def _build_v2_bundle(tmp_path: Path) -> Path:
    """v2 bundle (no mounts.jsonl) for back-compat test."""
    bundle_dir = tmp_path / "src"
    bundle_dir.mkdir()
    manifest = {
        "$schema": "https://nexus.io/schemas/manifest-v1.json",
        "format_version": "2.0.0",
        "bundle_id": "550e8400-e29b-41d4-a716-446655440000",
        "source_zone_id": "z1",
        "export_timestamp": "2026-01-01T00:00:00+00:00",
        "statistics": {
            "file_count": 0,
            "total_size_bytes": 0,
            "content_blob_count": 0,
            "permission_count": 0,
            "embedding_count": 0,
        },
        "options": {"include_content": True, "include_permissions": True},
        "checksums": {"algorithm": "sha256", "files": {}},
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest))
    out = tmp_path / "bundle.nexus"
    with tarfile.open(out, "w:gz") as tar:
        for p in bundle_dir.rglob("*"):
            tar.add(p, arcname=p.relative_to(bundle_dir))
    return out


def _maybe_run(result):
    """Wrap async results so the test works with either sync or async import_zone."""
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def test_import_without_overrides_raises_before_side_effects(tmp_path):
    bundle = _build_bundle_with_mount(tmp_path)
    fs = MagicMock()
    mgr = MagicMock()
    service = ZoneImportService(fs, mount_manager=mgr)

    options = ZoneImportOptions(bundle_path=bundle)
    with pytest.raises(MissingCredentialsError) as exc:
        _maybe_run(service.import_zone(options))
    assert "m-1" in exc.value.missing
    assert mgr.save_mount.call_count == 0
    assert mgr.update_mount.call_count == 0


def test_import_with_overrides_calls_save_mount(tmp_path):
    bundle = _build_bundle_with_mount(tmp_path)
    fs = MagicMock()
    mgr = MagicMock()
    mgr.get_mount.return_value = None
    service = ZoneImportService(fs, mount_manager=mgr)

    options = ZoneImportOptions(
        bundle_path=bundle,
        mount_overrides={"m-1": {"access_key_id": "AKIA", "secret_access_key": "wJalr"}},
    )
    _maybe_run(service.import_zone(options))
    assert mgr.save_mount.call_count == 1
    cfg = mgr.save_mount.call_args.kwargs["backend_config"]
    assert cfg["access_key_id"] == "AKIA"
    assert cfg["secret_access_key"] == "wJalr"


def test_import_restore_mounts_false_skips_mount_step(tmp_path):
    bundle = _build_bundle_with_mount(tmp_path)
    fs = MagicMock()
    mgr = MagicMock()
    service = ZoneImportService(fs, mount_manager=mgr)
    options = ZoneImportOptions(bundle_path=bundle, restore_mounts=False)
    _maybe_run(service.import_zone(options))
    assert mgr.save_mount.call_count == 0


def test_import_v2_bundle_no_mounts_jsonl_does_nothing(tmp_path):
    """v2 bundle (no mounts.jsonl) imports cleanly."""
    bundle = _build_v2_bundle(tmp_path)
    fs = MagicMock()
    mgr = MagicMock()
    service = ZoneImportService(fs, mount_manager=mgr)
    _maybe_run(service.import_zone(ZoneImportOptions(bundle_path=bundle)))
    assert mgr.save_mount.call_count == 0


def test_import_with_mounts_but_no_mount_manager_raises_loud(tmp_path):
    """Bundle contains mounts.jsonl but no mount_manager → loud ValueError, not buried in result.errors."""
    bundle = _build_bundle_with_mount(tmp_path)
    fs = MagicMock()
    service = ZoneImportService(fs)  # NO mount_manager
    options = ZoneImportOptions(
        bundle_path=bundle,
        mount_overrides={"m-1": {"access_key_id": "AKIA", "secret_access_key": "wJalr"}},
    )
    with pytest.raises(ValueError, match="MountManager"):
        _maybe_run(service.import_zone(options))
