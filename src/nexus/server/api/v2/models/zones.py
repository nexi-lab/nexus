"""API models for the /v2 zone surface (2C, §6).

Thin transport shapes over the contract objects: the wire truth is the
auth/common v1 schema (validated via nexus.contracts.zone_v1); these models
carry transport-specific dressing (cursor tokens, deprecation metadata)
without duplicating contract rules.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nexus.contracts.zone_v1 import (
    ExistingZoneIdRefStr,
    ResourceRef,
    ZoneDelegationScopeRule,
    ZonePathStr,
)


class ZoneView(BaseModel):
    """Zone as served by /v2 — the §4.4 object plus transport revision."""

    model_config = ConfigDict(extra="ignore")

    api_version: str = "auth.sudo.dev/v1"
    kind: str = "Zone"
    zone_id: str
    display_name: str
    description: str | None = None
    status: str
    deployment: dict[str, Any] | None = None
    labels: dict[str, str] | None = None
    revision: str
    created_by: dict[str, Any]
    created_at: str
    updated_at: str
    deleted_at: str | None = None


class ZoneCreateBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    zone_id: str = Field(min_length=3, max_length=63)
    display_name: str = Field(min_length=1)
    description: str | None = None
    deployment: dict[str, Any] | None = None
    labels: dict[str, str] | None = None


class ZonePatchBody(BaseModel):
    """Whitelist patch — unknown keys rejected at the contract layer."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    labels: dict[str, str] | None = None
    deployment: dict[str, Any] | None = None


class GrantView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    api_version: str = "auth.sudo.dev/v1"
    kind: str = "ZoneGrant"
    grant_id: str
    zone_id: str
    grantee: dict[str, Any]
    capabilities: list[str]
    resource_prefixes: list[str] | None = None
    source: dict[str, Any] | None = None
    issued_by: dict[str, Any]
    reason: str
    policy_version: str
    revision: str
    status: str
    created_at: str
    not_before: str | None = None
    expires_at: str | None = None
    revoked_at: str | None = None
    revoked_by: dict[str, Any] | None = None
    revoke_reason: str | None = None


class GrantCreateBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    grantee: dict[str, Any]
    capabilities: list[str] = Field(min_length=1)
    resource_prefixes: list[str] | None = None
    source: dict[str, Any] | None = None
    reason: str = Field(min_length=1)
    policy_version: str | None = None
    not_before: str | None = None
    expires_at: str | None = None


class GrantListResponse(BaseModel):
    grants: list[GrantView]
    next_cursor: str | None = None


class OperationView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    api_version: str = "auth.sudo.dev/v1"
    kind: str = "ZoneOperation"
    operation_id: str
    action: str
    zone_id: str | None = None
    grant_id: str | None = None
    state: str
    step: str
    retryable: bool
    error: dict[str, Any] | None = None
    created_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None


class DelegationIssueBody(BaseModel):
    """Trusted Moss issuance service only (§6.4)."""

    model_config = ConfigDict(extra="ignore")

    api_version: Literal["auth.sudo.dev/v1"] = "auth.sudo.dev/v1"
    kind: Literal["ZoneDelegationIssueRequest"] = "ZoneDelegationIssueRequest"
    user_id: str = Field(min_length=1)
    org_id: str = Field(min_length=1)
    membership_version: str = Field(min_length=1)
    zone_id: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    ttl_s: int = Field(default=900, ge=60, le=3600)
    grant_id: str | None = Field(default=None, min_length=1)
    purpose: Literal["data-access", "runtime"] = "data-access"
    scope_rules: list[ZoneDelegationScopeRule] | None = Field(default=None, min_length=1)


class DelegationView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    api_version: Literal["auth.sudo.dev/v1"] = "auth.sudo.dev/v1"
    kind: Literal["ZoneDelegation"] = "ZoneDelegation"
    delegation_id: str
    user_id: str
    org_id: str
    zone_id: str
    grant_id: str
    grant_revision: str
    authorization_epoch: int
    audience: str
    purpose: Literal["data-access", "runtime"] | None = None
    scope_rules: list[ZoneDelegationScopeRule] | None = Field(default=None, min_length=1)
    expires_at: str
    status: str


class ErrorInfoBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str
    message: str
    retryable: bool
    details: dict[str, Any] | None = None
    request_id: str | None = None


class ZoneListResponse(BaseModel):
    zones: list[ZoneView]
    next_cursor: str | None = None


class ZoneJoinBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    peers: list[str] = Field(min_length=1)
    learner: bool = False


class ZoneMountCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_zone_id: ExistingZoneIdRefStr
    target_zone_id: ExistingZoneIdRefStr
    path: ZonePathStr


class ZoneMountView(BaseModel):
    mount_id: str
    parent_zone_id: str
    target_zone_id: str
    path: str
    desired_state: str
    observed_state: str | None = None
    runtime_revision: str | None = None


class ZoneMountListResponse(BaseModel):
    mounts: list[ZoneMountView]
    next_cursor: str | None = None


class ZoneTransferBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: ResourceRef
    target: ResourceRef
