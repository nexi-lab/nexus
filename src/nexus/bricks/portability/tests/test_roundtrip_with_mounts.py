"""End-to-end roundtrip: export bundle with mounts → import → mounts restored.

Black-box test against real ZoneExportService + ZoneImportService. Uses
in-memory MountManager test doubles to avoid VFS plumbing for the kernel.
"""

import asyncio
import json
import tarfile
from unittest.mock import MagicMock

import pytest

from nexus.bricks.portability.export_service import ZoneExportService
from nexus.bricks.portability.import_service import ZoneImportService
from nexus.bricks.portability.models import (
    MissingCredentialsError,
    ZoneExportOptions,
    ZoneImportOptions,
)


def _ensure_registry() -> None:
    """Trigger connector registration so path_s3 is present in the live registry."""
    from nexus.backends import _register_optional_backends

    _register_optional_backends()


def _make_mount_manager_with(mounts: list[dict]) -> MagicMock:
    """Build a MountManager test double that returns `mounts` from list_mounts."""
    mgr = MagicMock()
    mgr.list_mounts.return_value = mounts
    mgr.get_mount.return_value = None  # no conflict
    return mgr


def _maybe_run(result):
    """Wrap async results so the test works with either sync or async services."""
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def test_roundtrip_export_then_import_with_overrides(tmp_path):
    pytest.importorskip("boto3")
    _ensure_registry()

    src_mgr = _make_mount_manager_with(
        [
            {
                "mount_id": "m-1",
                "mount_point": "/personal/alice",
                "backend_type": "path_s3",
                "backend_config": {
                    "bucket_name": "acme",
                    "access_key_id": "AKIA-LIVE-KEY",
                    "secret_access_key": "wJalr-LIVE-SECRET",
                },
                "owner_user_id": "alice",
                "zone_id": "z1",
                "description": None,
            },
        ]
    )

    # Provide a minimal kernel so metastore_list returns [] (no file records)
    kernel = MagicMock()
    kernel.metastore_list.return_value = []
    fs = MagicMock()
    fs._kernel = kernel

    out = tmp_path / "bundle.nexus"
    exporter = ZoneExportService(fs, mount_manager=src_mgr)
    _maybe_run(
        exporter.export_zone(
            "z1",
            ZoneExportOptions(
                output_path=out,
                include_mounts=True,
                sign=False,  # skip key-file lookup in unit test context
            ),
        )
    )
    assert out.exists()

    # Verify export stripped secrets — read raw mounts.jsonl back out of the tar.
    with tarfile.open(out, "r:gz") as tar:
        member = tar.getmember("mounts.jsonl")
        f = tar.extractfile(member)
        assert f is not None
        raw_bytes = f.read()
    record = json.loads(raw_bytes.decode())
    assert record["backend_config"]["access_key_id"] == "${MOUNT_m-1_ACCESS_KEY_ID}"
    assert record["backend_config"]["secret_access_key"] == "${MOUNT_m-1_SECRET_ACCESS_KEY}"
    # Critical: live secrets must not appear anywhere in the redacted record.
    assert b"AKIA-LIVE-KEY" not in raw_bytes
    assert b"wJalr-LIVE-SECRET" not in raw_bytes

    # Import without overrides → MissingCredentialsError
    dst_mgr = MagicMock()
    dst_mgr.get_mount.return_value = None
    importer = ZoneImportService(fs, mount_manager=dst_mgr)
    with pytest.raises(MissingCredentialsError):
        _maybe_run(importer.import_zone(ZoneImportOptions(bundle_path=out)))
    assert dst_mgr.save_mount.call_count == 0

    # Import with full overrides → save_mount called with concrete values
    _maybe_run(
        importer.import_zone(
            ZoneImportOptions(
                bundle_path=out,
                mount_overrides={
                    "m-1": {
                        "access_key_id": "AKIA-NEW-KEY",
                        "secret_access_key": "wJalr-NEW-SECRET",
                    }
                },
            )
        )
    )
    assert dst_mgr.save_mount.call_count == 1
    saved_cfg = dst_mgr.save_mount.call_args.kwargs["backend_config"]
    assert saved_cfg["access_key_id"] == "AKIA-NEW-KEY"
    assert saved_cfg["secret_access_key"] == "wJalr-NEW-SECRET"
    assert saved_cfg["bucket_name"] == "acme"  # non-secret survives roundtrip
