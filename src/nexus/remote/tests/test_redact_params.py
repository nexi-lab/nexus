"""Test the RPC params redactor (Issue #4083 round-2)."""

from nexus.remote.rpc_transport import _redact_params


def test_redact_params_passthrough_for_non_sensitive() -> None:
    p = {"zone_id": "z1", "include_content": True}
    assert _redact_params(p) == p


def test_redact_params_strips_mount_overrides_values() -> None:
    p = {
        "bundle_path": "/tmp/x.nexus",
        "mount_overrides": {
            "m-1": {"access_key_id": "AKIA-LIVE", "secret_access_key": "wJalr-LIVE"},
        },
    }
    out = _redact_params(p)
    assert out["bundle_path"] == "/tmp/x.nexus"  # untouched
    assert out["mount_overrides"]["m-1"]["access_key_id"] == "***"
    assert out["mount_overrides"]["m-1"]["secret_access_key"] == "***"
    assert "AKIA-LIVE" not in str(out)
    assert "wJalr-LIVE" not in str(out)


def test_redact_params_strips_auth_token_value() -> None:
    out = _redact_params({"auth_token": "sk-secret-live"})
    assert out["auth_token"] == "***"
    assert "sk-secret-live" not in str(out)


def test_redact_params_handles_none_mount_overrides() -> None:
    out = _redact_params({"mount_overrides": None})
    assert out["mount_overrides"] is None  # don't redact None


def test_redact_params_non_dict_passthrough() -> None:
    assert _redact_params("not a dict") == "not a dict"
    assert _redact_params(None) is None
