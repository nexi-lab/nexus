"""Unit tests for redaction.py."""

from unittest.mock import patch

import pytest

from nexus.bricks.portability.models import (
    PlaceholderRef,
    SensitiveFieldNotDeclaredError,
)
from nexus.bricks.portability.redaction import (
    SECRET_SHAPED,
    _get_connection_args,
    audit_backend,
    declared_secret_fields,
    redact_config,
)
from nexus.extensions.types import ArgType, ConnectionArg


def _ensure_registry() -> None:
    """Trigger connector registration so path_s3 is present in the live registry."""
    from nexus.backends import _register_optional_backends

    _register_optional_backends()


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
# declared_secret_fields — live registry
# ---------------------------------------------------------------------------


def test_declared_secret_fields_for_path_s3():
    pytest.importorskip("boto3")
    _ensure_registry()
    fields = declared_secret_fields("path_s3")
    assert "access_key_id" in fields
    assert "secret_access_key" in fields
    assert "session_token" in fields
    assert "bucket_name" not in fields


# ---------------------------------------------------------------------------
# audit_backend — live registry
# ---------------------------------------------------------------------------


def test_audit_backend_passes_for_path_s3():
    """All secret-shaped names in path_s3 are already marked secret=True."""
    pytest.importorskip("boto3")
    _ensure_registry()
    assert audit_backend("path_s3") == []


# ---------------------------------------------------------------------------
# redact_config — live registry
# ---------------------------------------------------------------------------


def test_redact_config_replaces_only_declared_secrets():
    pytest.importorskip("boto3")
    _ensure_registry()
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


def test_redact_config_skips_none_values():
    pytest.importorskip("boto3")
    _ensure_registry()
    config = {"bucket_name": "acme", "access_key_id": None}
    redacted, placeholders = redact_config("path_s3", config, mount_id="m-1")
    assert redacted["access_key_id"] is None
    assert placeholders == []


def test_redact_config_idempotent_on_already_redacted():
    pytest.importorskip("boto3")
    _ensure_registry()
    config = {"bucket_name": "acme", "access_key_id": "${MOUNT_m-1_ACCESS_KEY_ID}"}
    redacted, placeholders = redact_config("path_s3", config, mount_id="m-1")
    assert redacted["access_key_id"] == "${MOUNT_m-1_ACCESS_KEY_ID}"
    assert placeholders == []  # no duplicate placeholder for already-redacted field


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


def test_redact_config_unknown_backend_raises():
    """Refusing to ship a mount whose CONNECTION_ARGS contract is unknown.

    Without this guard, a slim install (where e.g. boto3 isn't installed and
    path_s3 isn't loaded) would silently produce a bundle with cleartext S3
    credentials because the registry returns no secret-fields set. The export
    must abort instead.
    """
    with patch(
        "nexus.bricks.portability.redaction._get_connection_args",
        return_value={},
    ):
        with pytest.raises(SensitiveFieldNotDeclaredError) as exc:
            redact_config(
                "path_s3",
                {"access_key_id": "AKIA-LIVE", "bucket_name": "acme"},
                mount_id="m-1",
            )
        # Sorted for deterministic output; offending fields are surfaced so
        # the operator knows what was about to leak.
        assert exc.value.fields == ["access_key_id", "bucket_name"]
        assert "path_s3" in exc.value.backend_type
        assert "install the matching extra" in exc.value.backend_type


def test_placeholder_field_dotted_path_is_predictable():
    pytest.importorskip("boto3")
    _ensure_registry()
    config = {"access_key_id": "AKIA"}
    _, placeholders = redact_config("path_s3", config, mount_id="m-1")
    assert placeholders[0].field == "mounts.m-1.access_key_id"


def test_audit_safe_field_passes_audit():
    """ConnectionArg(audit_safe=True) on a secret-shaped name silences the audit."""
    from unittest.mock import patch

    from nexus.extensions.types import ArgType, ConnectionArg

    fake_args = {
        "secret_path": ConnectionArg(
            type=ArgType.PATH,
            description="path to a secrets dir, not a secret value",
            audit_safe=True,
        ),
    }

    with patch("nexus.bricks.portability.redaction._get_connection_args", return_value=fake_args):
        assert audit_backend("fake") == []


def test_get_connection_args_finds_slack_manifest_args():
    """_get_connection_args must surface ConnectorManifest.connection_args
    (Slack-style), not just class-level CONNECTION_ARGS."""
    pytest.importorskip("slack_sdk")
    _ensure_registry()
    args = _get_connection_args("slack_connector")
    assert "token_manager_db" in args, (
        "Slack connector_args missed by _get_connection_args — extension-store "
        "manifest fallback may be broken."
    )


def test_redact_config_rejects_undeclared_secret_shaped_top_level_key():
    """Round 3: persisted backend_config can hold extra keys not in
    CONNECTION_ARGS. If one looks like a secret (e.g., a path_local
    record polluted with `secret_access_key`), the export must abort
    rather than ship it cleartext."""
    pytest.importorskip("boto3")
    _ensure_registry()
    config = {
        "root_path": "/tmp/data",
        "secret_access_key": "AKIA-LIVE-LEAK",  # not in path_local CONNECTION_ARGS
    }
    with pytest.raises(SensitiveFieldNotDeclaredError) as exc:
        redact_config("path_local", config, mount_id="m-leak")
    assert "secret_access_key" in exc.value.fields


def test_redact_config_rejects_nested_secret_shaped_key():
    """Round 3: nested credential dicts (e.g., a `metadata` blob
    containing `auth_token`) must also fail closed — CONNECTION_ARGS
    can't declare structure for a nested dict, so any nested
    secret-shaped key is treated as a leak."""
    pytest.importorskip("boto3")
    _ensure_registry()
    config = {
        "root_path": "/tmp/data",
        "metadata": {"description": "ok", "auth_token": "LIVE-NESTED"},
    }
    with pytest.raises(SensitiveFieldNotDeclaredError) as exc:
        redact_config("path_local", config, mount_id="m-leak")
    assert any("auth_token" in f for f in exc.value.fields), exc.value.fields
