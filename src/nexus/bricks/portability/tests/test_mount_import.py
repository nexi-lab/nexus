"""Tests for mount_import.py."""

import json
from unittest.mock import MagicMock

import pytest

from nexus.bricks.portability.models import (
    ConflictMode,
    MissingCredentialsError,
    MountRecord,
)
from nexus.bricks.portability.mount_import import (
    import_mounts,
    materialize,
    read_mounts,
    validate_overrides,
)


@pytest.fixture
def redacted_record():
    return MountRecord(
        mount_id="m-1",
        mount_point="/personal/alice",
        backend_type="path_s3",
        backend_config={
            "bucket_name": "acme",
            "access_key_id": "${MOUNT_m-1_ACCESS_KEY_ID}",
            "secret_access_key": "${MOUNT_m-1_SECRET_ACCESS_KEY}",
        },
        owner_user_id="alice",
        zone_id="acme",
        description=None,
    )


def test_read_mounts_absent_file_returns_empty(tmp_path):
    assert read_mounts(tmp_path) == []


def test_read_mounts_parses_jsonl(tmp_path, redacted_record):
    p = tmp_path / "mounts.jsonl"
    p.write_text(json.dumps(redacted_record.to_dict()) + "\n")
    out = read_mounts(tmp_path)
    assert len(out) == 1
    assert out[0] == redacted_record


def test_read_mounts_skips_blank_lines(tmp_path, redacted_record):
    p = tmp_path / "mounts.jsonl"
    p.write_text(json.dumps(redacted_record.to_dict()) + "\n\n")
    assert len(read_mounts(tmp_path)) == 1


def test_validate_overrides_no_redacted_fields_passes():
    rec = MountRecord(
        mount_id="m-1",
        mount_point="/x",
        backend_type="path_local",
        backend_config={"root": "/data"},
    )
    validate_overrides([rec], overrides=None)  # no raise


def test_validate_overrides_missing_raises_with_all_gaps(redacted_record):
    rec2 = MountRecord(
        mount_id="m-2",
        mount_point="/y",
        backend_type="path_s3",
        backend_config={"access_key_id": "${MOUNT_m-2_ACCESS_KEY_ID}"},
    )
    with pytest.raises(MissingCredentialsError) as exc:
        validate_overrides([redacted_record, rec2], overrides=None)
    missing = exc.value.missing
    assert set(missing.keys()) == {"m-1", "m-2"}
    assert "access_key_id" in missing["m-1"]
    assert "secret_access_key" in missing["m-1"]
    assert "access_key_id" in missing["m-2"]


def test_validate_overrides_partial_provided_still_raises(redacted_record):
    overrides = {"m-1": {"access_key_id": "AKIA"}}  # missing secret_access_key
    with pytest.raises(MissingCredentialsError) as exc:
        validate_overrides([redacted_record], overrides=overrides)
    assert exc.value.missing == {"m-1": ["secret_access_key"]}


def test_validate_overrides_full_passes(redacted_record):
    overrides = {"m-1": {"access_key_id": "AKIA", "secret_access_key": "wJalr"}}
    validate_overrides([redacted_record], overrides=overrides)  # no raise


def test_materialize_substitutes_placeholders(redacted_record):
    overrides = {"access_key_id": "AKIA", "secret_access_key": "wJalr"}
    out = materialize(redacted_record, overrides)
    assert out["access_key_id"] == "AKIA"
    assert out["secret_access_key"] == "wJalr"
    assert out["bucket_name"] == "acme"


def test_import_mounts_calls_save_mount_per_record(redacted_record):
    mgr = MagicMock()
    mgr.get_mount.return_value = None  # no conflict
    overrides = {"m-1": {"access_key_id": "AKIA", "secret_access_key": "wJalr"}}
    errors = import_mounts(
        mounts=[redacted_record],
        overrides=overrides,
        mount_manager=mgr,
        target_zone_id=None,
        conflict_mode=ConflictMode.SKIP,
    )
    assert errors == []
    assert mgr.save_mount.call_count == 1
    kwargs = mgr.save_mount.call_args.kwargs
    assert kwargs["mount_point"] == "/personal/alice"
    assert kwargs["backend_type"] == "path_s3"
    assert kwargs["backend_config"]["access_key_id"] == "AKIA"


def test_import_mounts_skip_existing_records_info(redacted_record):
    mgr = MagicMock()
    mgr.get_mount.return_value = {"mount_point": "/personal/alice"}  # already there
    errors = import_mounts(
        mounts=[redacted_record],
        overrides={"m-1": {"access_key_id": "AKIA", "secret_access_key": "w"}},
        mount_manager=mgr,
        target_zone_id=None,
        conflict_mode=ConflictMode.SKIP,
    )
    assert mgr.save_mount.call_count == 0
    assert len(errors) == 1
    assert "alice" in errors[0].message


def test_import_mounts_overwrite_calls_update(redacted_record):
    mgr = MagicMock()
    mgr.get_mount.return_value = {"mount_point": "/personal/alice"}
    import_mounts(
        mounts=[redacted_record],
        overrides={"m-1": {"access_key_id": "AKIA", "secret_access_key": "w"}},
        mount_manager=mgr,
        target_zone_id=None,
        conflict_mode=ConflictMode.OVERWRITE,
    )
    mgr.update_mount.assert_called_once()
    assert mgr.save_mount.call_count == 0


def test_import_mounts_zone_remap_applied(redacted_record):
    mgr = MagicMock()
    mgr.get_mount.return_value = None
    import_mounts(
        mounts=[redacted_record],
        overrides={"m-1": {"access_key_id": "AKIA", "secret_access_key": "w"}},
        mount_manager=mgr,
        target_zone_id="new-zone",
        conflict_mode=ConflictMode.SKIP,
    )
    assert mgr.save_mount.call_args.kwargs["zone_id"] == "new-zone"


def test_import_mounts_orders_by_path_depth():
    """Parents must restore before children to avoid ordering bugs."""
    mgr = MagicMock()
    mgr.get_mount.return_value = None
    deep = MountRecord(
        mount_id="m-deep",
        mount_point="/personal/alice/sub",
        backend_type="path_local",
        backend_config={"root": "/x"},
    )
    shallow = MountRecord(
        mount_id="m-shallow",
        mount_point="/personal",
        backend_type="path_local",
        backend_config={"root": "/y"},
    )
    import_mounts(
        mounts=[deep, shallow],
        overrides={},
        mount_manager=mgr,
        target_zone_id=None,
        conflict_mode=ConflictMode.SKIP,
    )
    saved_paths = [c.kwargs["mount_point"] for c in mgr.save_mount.call_args_list]
    assert saved_paths.index("/personal") < saved_paths.index("/personal/alice/sub")
