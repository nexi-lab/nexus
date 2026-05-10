"""Tests for mount_export.py."""

import json
from unittest.mock import MagicMock

import pytest

from nexus.bricks.portability.models import (
    PlaceholderRef,
    SensitiveFieldNotDeclaredError,
)
from nexus.bricks.portability.mount_export import (
    collect_mounts,
    redact_and_write,
)


@pytest.fixture
def s3_mount_dict():
    return {
        "mount_id": "m-1",
        "mount_point": "/personal/alice",
        "backend_type": "path_s3",
        "backend_config": {
            "bucket_name": "acme",
            "access_key_id": "AKIA1234",
            "secret_access_key": "wJalr...",
        },
        "owner_user_id": "alice",
        "zone_id": "acme",
        "description": None,
    }


def test_collect_mounts_calls_list_mounts_with_zone_filter(s3_mount_dict):
    mgr = MagicMock()
    mgr.list_mounts.return_value = [s3_mount_dict]
    out = collect_mounts(mgr, zone_id="acme")
    mgr.list_mounts.assert_called_once_with(zone_id="acme")
    assert out == [s3_mount_dict]


def test_collect_mounts_no_zone_filter():
    mgr = MagicMock()
    mgr.list_mounts.return_value = []
    collect_mounts(mgr, zone_id=None)
    mgr.list_mounts.assert_called_once_with(zone_id=None)


def test_redact_and_write_redacts_secrets_and_returns_placeholders(tmp_path, s3_mount_dict):
    pytest.importorskip("boto3")
    from nexus.bricks.portability.tests.test_redaction_unit import _ensure_registry

    _ensure_registry()

    out_path = tmp_path / "mounts.jsonl"
    placeholders = redact_and_write([s3_mount_dict], out_path=out_path)

    assert out_path.exists()
    line = out_path.read_text().strip()
    record = json.loads(line)
    assert record["backend_config"]["access_key_id"] == "${MOUNT_m-1_ACCESS_KEY_ID}"
    assert record["backend_config"]["secret_access_key"] == "${MOUNT_m-1_SECRET_ACCESS_KEY}"
    assert record["backend_config"]["bucket_name"] == "acme"  # not redacted

    assert {p.name for p in placeholders} == {
        "MOUNT_m-1_ACCESS_KEY_ID",
        "MOUNT_m-1_SECRET_ACCESS_KEY",
    }
    assert all(isinstance(p, PlaceholderRef) for p in placeholders)


def test_redact_and_write_sorts_lines_by_mount_id(tmp_path):
    from nexus.bricks.portability.tests.test_redaction_unit import _ensure_registry

    _ensure_registry()

    mounts: list[dict[str, object]] = [
        {
            "mount_id": "m-z",
            "mount_point": "/z",
            "backend_type": "path_local",
            "backend_config": {},
            "owner_user_id": None,
            "zone_id": None,
            "description": None,
        },
        {
            "mount_id": "m-a",
            "mount_point": "/a",
            "backend_type": "path_local",
            "backend_config": {},
            "owner_user_id": None,
            "zone_id": None,
            "description": None,
        },
    ]
    out_path = tmp_path / "mounts.jsonl"
    redact_and_write(mounts, out_path=out_path)
    lines = out_path.read_text().strip().split("\n")
    assert json.loads(lines[0])["mount_id"] == "m-a"
    assert json.loads(lines[1])["mount_id"] == "m-z"


def test_redact_and_write_byte_stable_across_runs(tmp_path, s3_mount_dict):
    pytest.importorskip("boto3")
    from nexus.bricks.portability.tests.test_redaction_unit import _ensure_registry

    _ensure_registry()

    out1 = tmp_path / "a.jsonl"
    out2 = tmp_path / "b.jsonl"
    redact_and_write([s3_mount_dict], out_path=out1)
    redact_and_write([s3_mount_dict], out_path=out2)
    assert out1.read_bytes() == out2.read_bytes()


def test_redact_and_write_audit_failure_raises(tmp_path):
    """If a mount references a backend whose CONNECTION_ARGS audit fails, raise."""
    from unittest.mock import patch

    bad_mount = {
        "mount_id": "m-1",
        "mount_point": "/x",
        "backend_type": "path_s3",
        "backend_config": {"my_token": "x"},
        "owner_user_id": None,
        "zone_id": None,
        "description": None,
    }
    with (
        patch(
            "nexus.bricks.portability.redaction.audit_backend",
            return_value=["my_token"],
        ),
        pytest.raises(SensitiveFieldNotDeclaredError),
    ):
        redact_and_write([bad_mount], out_path=tmp_path / "x.jsonl")


def test_redact_and_write_empty_list_writes_empty_file(tmp_path):
    out_path = tmp_path / "mounts.jsonl"
    placeholders = redact_and_write([], out_path=out_path)
    assert out_path.exists()
    assert out_path.read_text() == ""
    assert placeholders == []
