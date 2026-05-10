"""Unit tests for redaction.py."""

from unittest.mock import patch

import pytest

from nexus.bricks.portability.models import (
    PlaceholderRef,
    SensitiveFieldNotDeclaredError,
)
from nexus.bricks.portability.redaction import (
    SECRET_SHAPED,
    audit_backend,
    declared_secret_fields,
    redact_config,
)
from nexus.extensions.types import ArgType, ConnectionArg

# ---------------------------------------------------------------------------
# Fake CONNECTION_ARGS for "path_s3" — mirrors the real backend's declaration
# so these tests remain unit tests independent of connector registration.
# ---------------------------------------------------------------------------
_PATH_S3_ARGS = {
    "bucket_name": ConnectionArg(type=ArgType.STRING, description="S3 bucket name", secret=False),
    "region_name": ConnectionArg(type=ArgType.STRING, description="AWS region", secret=False),
    "credentials_path": ConnectionArg(
        type=ArgType.PATH, description="Credentials path", secret=True
    ),
    "prefix": ConnectionArg(type=ArgType.STRING, description="Key prefix", secret=False),
    "access_key_id": ConnectionArg(
        type=ArgType.SECRET, description="AWS access key ID", secret=True
    ),
    "secret_access_key": ConnectionArg(
        type=ArgType.PASSWORD, description="AWS secret key", secret=True
    ),
    "session_token": ConnectionArg(
        type=ArgType.SECRET, description="AWS session token", secret=True
    ),
}


@pytest.fixture()
def path_s3_args(monkeypatch):
    """Patch _get_connection_args to return the real path_s3 CONNECTION_ARGS shape.

    Uses monkeypatch (not unittest.mock.patch) to avoid pytest-mock interference
    with fixture-level patching. This makes tests independent of ConnectorRegistry
    registration state, which differs between the worktree src and site-packages.
    """
    import nexus.bricks.portability.redaction as _redaction_mod

    monkeypatch.setattr(_redaction_mod, "_get_connection_args", lambda _: _PATH_S3_ARGS)


# ---------------------------------------------------------------------------
# SECRET_SHAPED regex — no registry required
# ---------------------------------------------------------------------------


def test_secret_shape_regex_matches_obvious_names():
    for name in ("api_key", "secret_access_key", "session_token", "password", "credential"):
        assert SECRET_SHAPED.search(name), name


def test_secret_shape_regex_ignores_benign_names():
    for name in ("bucket_name", "region", "prefix", "path"):
        assert not SECRET_SHAPED.search(name), name


# ---------------------------------------------------------------------------
# declared_secret_fields
# ---------------------------------------------------------------------------


def test_declared_secret_fields_for_path_s3(path_s3_args):  # noqa: ARG001
    fields = declared_secret_fields("path_s3")
    assert "access_key_id" in fields
    assert "secret_access_key" in fields
    assert "session_token" in fields
    assert "bucket_name" not in fields


# ---------------------------------------------------------------------------
# audit_backend
# ---------------------------------------------------------------------------


def test_audit_backend_passes_for_path_s3(path_s3_args):  # noqa: ARG001
    """All secret-shaped names in path_s3 are already marked secret=True."""
    assert audit_backend("path_s3") == []


# ---------------------------------------------------------------------------
# redact_config
# ---------------------------------------------------------------------------


def test_redact_config_replaces_only_declared_secrets(path_s3_args):  # noqa: ARG001
    config = {
        "bucket_name": "acme",
        "access_key_id": "AKIA1234",
        "secret_access_key": "wJalr...",
    }
    redacted, placeholders = redact_config("path_s3", config, mount_id="m-1")
    assert redacted["bucket_name"] == "acme"
    assert redacted["access_key_id"] == "${MOUNT_m-1_ACCESS_KEY_ID}"
    assert redacted["secret_access_key"] == "${MOUNT_m-1_SECRET_ACCESS_KEY}"
    assert {p.name for p in placeholders} == {
        "MOUNT_m-1_ACCESS_KEY_ID",
        "MOUNT_m-1_SECRET_ACCESS_KEY",
    }
    assert all(isinstance(p, PlaceholderRef) for p in placeholders)


def test_redact_config_skips_none_values(path_s3_args):  # noqa: ARG001
    config = {"bucket_name": "acme", "access_key_id": None}
    redacted, placeholders = redact_config("path_s3", config, mount_id="m-1")
    assert redacted["access_key_id"] is None
    assert placeholders == []


def test_redact_config_idempotent_on_already_redacted(path_s3_args):  # noqa: ARG001
    config = {"bucket_name": "acme", "access_key_id": "${MOUNT_m-1_ACCESS_KEY_ID}"}
    redacted, _ = redact_config("path_s3", config, mount_id="m-1")
    assert redacted["access_key_id"] == "${MOUNT_m-1_ACCESS_KEY_ID}"


def test_redact_config_audit_failure_raises():
    """If a backend has a secret-shaped name not marked secret=True, raise."""
    fake_args = {
        "bucket": ConnectionArg(type=ArgType.STRING, description="ok"),
        "my_token": ConnectionArg(
            type=ArgType.STRING,
            description="should be secret but isn't",
            secret=False,
        ),
    }

    with patch("nexus.bricks.portability.redaction._get_connection_args", return_value=fake_args):
        with pytest.raises(SensitiveFieldNotDeclaredError) as exc:
            redact_config("fake", {"bucket": "x", "my_token": "y"}, mount_id="m-1")
        assert "my_token" in exc.value.fields


def test_placeholder_field_dotted_path_is_predictable(path_s3_args):  # noqa: ARG001
    config = {"access_key_id": "AKIA"}
    _, placeholders = redact_config("path_s3", config, mount_id="m-1")
    assert placeholders[0].field == "mounts.m-1.access_key_id"
