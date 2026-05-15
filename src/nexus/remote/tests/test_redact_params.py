"""Tests for the recursive RPC params redactor (Issue #4083 rounds 2-3)."""

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


def test_redact_params_recurses_into_nested_headers() -> None:
    """Round 3: real RPC params carry credentials in nested headers / env
    dicts. The redactor must walk into them, not just hit top-level keys."""
    p = {
        "headers": {"Authorization": "Bearer LIVE-TOKEN", "X-Custom": "ok"},
        "env": {"API_KEY": "LIVE", "PATH": "/usr/bin"},
    }
    out = _redact_params(p)
    assert out["headers"]["Authorization"] == "***"
    assert out["headers"]["X-Custom"] == "ok"
    assert out["env"]["API_KEY"] == "***"
    assert out["env"]["PATH"] == "/usr/bin"
    assert "LIVE-TOKEN" not in str(out)
    assert "LIVE" not in str(out)


def test_redact_params_redacts_sandbox_and_nexus_api_keys() -> None:
    """Round 3: sandbox_connect / sandbox_api_key / nexus_api_key params
    were missed by the round-2 top-level allowlist."""
    p = {"sandbox_api_key": "sandbox-LIVE", "nexus_api_key": "nexus-LIVE"}
    out = _redact_params(p)
    assert out["sandbox_api_key"] == "***"
    assert out["nexus_api_key"] == "***"
    assert "sandbox-LIVE" not in str(out)
    assert "nexus-LIVE" not in str(out)


def test_redact_params_recurses_into_lists() -> None:
    """Lists of dicts (e.g., headers list) must be walked too."""
    p = {"items": [{"api_key": "LIVE"}, {"name": "ok"}]}
    out = _redact_params(p)
    assert out["items"][0]["api_key"] == "***"
    assert out["items"][1]["name"] == "ok"
    assert "LIVE" not in str(out)


def test_redact_params_does_not_mutate_input() -> None:
    p: dict[str, object] = {"auth_token": "LIVE", "nested": {"api_key": "LIVE2"}}
    _redact_params(p)
    assert p["auth_token"] == "LIVE"
    nested = p["nested"]
    assert isinstance(nested, dict)
    assert nested["api_key"] == "LIVE2"
