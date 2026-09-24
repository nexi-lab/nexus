"""Adapter-level conformance for nexus.contracts.zone_v1 (owner-local,
conformance-verified — not codegen'd): open registries, whitelist patch,
unknown-major rejection and the model dispatch table."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus.contracts.zone_v1 import (
    KNOWN_CAPABILITIES,
    KNOWN_ERROR_CODES,
    ZONE_V1_MODELS,
    ErrorInfo,
    ZoneGrant,
    ZoneOperation,
    ZonePatchRequest,
)

ZONE_GRANT_MINIMAL = {
    "api_version": "auth.sudo.dev/v1",
    "kind": "ZoneGrant",
    "grant_id": "g",
    "zone_id": "cloud-user-1001",
    "grantee": {"subject_type": "user", "subject_id": "u"},
    "capabilities": ["zone.data.read"],
    "issued_by": {"subject_type": "user", "subject_id": "u"},
    "reason": "r",
    "policy_version": "p",
    "revision": "r",
    "status": "active",
    "created_at": "2026-09-18T08:00:00Z",
}

OPERATION_MINIMAL = {
    "api_version": "auth.sudo.dev/v1",
    "kind": "ZoneOperation",
    "operation_id": "o",
    "action": "create",
    "state": "queued",
    "step": "validate",
    "retryable": True,
    "created_at": "2026-09-18T08:00:00Z",
    "updated_at": "2026-09-18T08:00:00Z",
}


def test_dispatch_table_covers_all_wire_kinds() -> None:
    assert set(ZONE_V1_MODELS) == {
        "PrincipalRef",
        "ResourceRef",
        "Zone",
        "ZoneCreateRequest",
        "ZonePatchRequest",
        "ZoneGrant",
        "ZoneGrantCreateRequest",
        "ZoneOperation",
        "ZoneDelegationScopeRule",
        "ZoneDelegationIssueRequest",
        "ZoneDelegation",
        "RuntimeResourceScope",
    }


def test_unknown_major_rejected() -> None:
    payload = dict(OPERATION_MINIMAL, api_version="auth.sudo.dev/v2")
    with pytest.raises(ValidationError):
        ZoneOperation.model_validate(payload)


def test_open_capability_registry_has_unknown_fallback() -> None:
    grant = ZoneGrant.model_validate(
        dict(ZONE_GRANT_MINIMAL, capabilities=["zone.data.read", "zone.brand.new"])
    )
    assert grant.has_unknown_capabilities is True
    known = ZoneGrant.model_validate(
        dict(ZONE_GRANT_MINIMAL, capabilities=sorted(KNOWN_CAPABILITIES))
    )
    assert known.has_unknown_capabilities is False


def test_open_error_code_registry_has_unknown_fallback() -> None:
    err = ErrorInfo.model_validate({"code": "SOMETHING_NEW", "message": "m", "retryable": False})
    assert err.is_unknown_code is True
    known = ErrorInfo.model_validate(
        {"code": sorted(KNOWN_ERROR_CODES)[0], "message": "m", "retryable": False}
    )
    assert known.is_unknown_code is False


def test_patch_whitelist_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        ZonePatchRequest.model_validate(
            {
                "api_version": "auth.sudo.dev/v1",
                "kind": "ZonePatchRequest",
                "zone_id": "renamed-attempt",
            }
        )


def test_patch_whitelist_rejects_trust_domain() -> None:
    with pytest.raises(ValidationError):
        ZonePatchRequest.model_validate(
            {
                "api_version": "auth.sudo.dev/v1",
                "kind": "ZonePatchRequest",
                "deployment": {"trust_domain": "attacker.example"},
            }
        )


def test_models_ignore_secret_styled_unknown_fields() -> None:
    grant = ZoneGrant.model_validate(dict(ZONE_GRANT_MINIMAL, api_key="sk-live", token="t"))
    dumped = grant.model_dump(mode="json")
    assert "api_key" not in dumped and "token" not in dumped


def test_size_bytes_stays_a_decimal_string() -> None:
    op = ZoneOperation.model_validate(
        dict(
            OPERATION_MINIMAL,
            result={"size_bytes": "9007199254740993"},
        )
    )
    assert isinstance(op.result, dict) and op.result["size_bytes"] == "9007199254740993"
