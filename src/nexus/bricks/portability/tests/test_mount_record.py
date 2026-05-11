"""Tests for MountRecord dataclass."""

import pytest

from nexus.bricks.portability.models import (
    MissingCredentialsError,
    MountRecord,
    SensitiveFieldNotDeclaredError,
)


def test_mount_record_round_trip_dict():
    rec = MountRecord(
        mount_id="m-1",
        mount_point="/personal/alice",
        backend_type="path_s3",
        backend_config={"bucket_name": "acme", "access_key_id": "${MOUNT_m-1_ACCESS_KEY_ID}"},
        owner_user_id="alice",
        zone_id="acme",
        description=None,
    )
    d = rec.to_dict()
    rec2 = MountRecord.from_dict(d)
    assert rec2 == rec


def test_mount_record_handles_none_zone_and_owner():
    rec = MountRecord(
        mount_id="m-2",
        mount_point="/team/x",
        backend_type="path_local",
        backend_config={"root": "/data"},
        owner_user_id=None,
        zone_id=None,
        description="team mount",
    )
    rec2 = MountRecord.from_dict(rec.to_dict())
    assert rec2 == rec


def test_sensitive_field_not_declared_error_carries_payload():
    err = SensitiveFieldNotDeclaredError(backend_type="path_s3", fields=["my_secret"])
    assert err.backend_type == "path_s3"
    assert err.fields == ["my_secret"]
    assert "path_s3" in str(err)
    assert "my_secret" in str(err)


def test_missing_credentials_error_lists_all_gaps():
    err = MissingCredentialsError(missing={"m-1": ["a", "b"], "m-2": ["c"]})
    msg = str(err)
    assert "m-1" in msg and "a" in msg and "b" in msg
    assert "m-2" in msg and "c" in msg


def test_missing_credentials_error_is_value_error():
    with pytest.raises(ValueError):
        raise MissingCredentialsError(missing={"m-1": ["a"]})
