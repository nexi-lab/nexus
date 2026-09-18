"""Owner-local Python adapter for the zone-v1 product contracts (auth/common v1).

Conformance-verified, not codegen'd: the JSON Schemas under ``contracts/``
are the wire SSOT and ``tests/contracts`` locks this module to them via the
shared fixtures — every valid fixture must parse to an equivalent model and
every invalid fixture must raise. Unknown optional fields are ignored at the
object-model layer (wire proxies may still preserve them); the patch request
is the one shape that forbids unknown keys, matching its whitelist contract.

This module must not be consumed by sudostack or any other repo: Nexus owns
these definitions and derives its own adapter locally.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Annotated, Literal

from jsonschema import Draft202012Validator
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

RFC3339_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
DECIMAL_STRING_PATTERN = r"^[0-9]+$"
DIGEST_PATTERN = r"^[a-z0-9-]+:[A-Za-z0-9+/=._-]+$"
CAPABILITY_PATTERN = r"^zone\.[a-z-]+\.[a-z-]+$"

_VENDOR_DIR = Path(__file__).resolve().parents[3] / "contracts" / "vendor" / "nexus-vfs.gen"


@cache
def _projection_validator(filename: str) -> Draft202012Validator:
    """Compile a vendored nexus-vfs projection once.

    The zone-id/zone-path rules live in the vendored projection files; this
    adapter delegates to them instead of restating the regexes, so schema and
    model can never disagree about what a zone id or path is.
    """
    doc = json.loads((_VENDOR_DIR / filename).read_text(encoding="utf-8"))
    return Draft202012Validator(doc)


def _via_projection(filename: str, value: str) -> str:
    if not _projection_validator(filename).is_valid(value):
        raise ValueError(f"{value!r} fails the {filename} projection")
    return value


#: Zone identity as an existing/historical reference (kernel-owned ids allowed).
ExistingZoneIdRefStr = Annotated[
    str, AfterValidator(lambda v: _via_projection("existing-zone-id-ref.schema.gen.json", v))
]
#: Strict tenant-create admission candidate (kernel-owned ids rejected).
TenantZoneIdCreateStr = Annotated[
    str, AfterValidator(lambda v: _via_projection("tenant-zone-id-create.schema.gen.json", v))
]
#: Zone-relative absolute path (component/depth/reserved-prefix rules).
ZonePathStr = Annotated[
    str, AfterValidator(lambda v: _via_projection("zone-path.schema.gen.json", v))
]

#: Open registry of known capabilities (§4.6). Unknown codes are legal wire
#: values within the same major; this list exists for callers that want the
#: known constants, not to close the set.
KNOWN_CAPABILITIES: frozenset[str] = frozenset(
    {
        "zone.data.read",
        "zone.data.write",
        "zone.runtime.execute",
        "zone.metadata.manage",
        "zone.grants.manage",
        "zone.data.export",
        "zone.lifecycle.delete",
    }
)

#: Fixed known error codes (§4.10). The registry is open: well-formed unknown
#: codes must survive parsing, and clients judge by code/retryable only.
KNOWN_ERROR_CODES: frozenset[str] = frozenset(
    {
        "INVALID_ZONE_ID",
        "RESERVED_ZONE_ID",
        "ZONE_ALREADY_EXISTS",
        "ZONE_NOT_FOUND",
        "ZONE_IMMUTABLE_FIELD",
        "ZONE_REVISION_CONFLICT",
        "ZONE_NOT_ACTIVE",
        "ZONE_IN_USE",
        "ZONE_DELETE_BLOCKED",
        "ZONE_RUNTIME_UNAVAILABLE",
        "ZONE_QUORUM_UNAVAILABLE",
        "GRANT_NOT_FOUND",
        "GRANT_NOT_ACTIVE",
        "GRANT_REVOKED",
        "GRANT_EXPIRED",
        "RESOURCE_RELATION_DENIED",
        "IDEMPOTENCY_CONFLICT",
        "UNSUPPORTED_CONTRACT_MAJOR",
        "UNSUPPORTED_CAPABILITY",
        "PROJECTION_PENDING",
        "PROJECTION_FAILED",
    }
)


class PrincipalRef(BaseModel):
    """Identity reference (§4.3). ``subject_id``/``trust_domain`` are not credentials."""

    subject_type: Literal["organization", "user", "agent", "service"]
    subject_id: str = Field(min_length=1)
    trust_domain: str | None = Field(default=None, min_length=1)


class ResourceRef(BaseModel):
    """Product wire identity (§4.7). ``zone_id + path`` jointly form the identity.

    Format rules for zone_id/path are owned by nexus-vfs projections; this
    model carries them and enforces only product-level shapes (digest prefix,
    decimal-string size).
    """

    model_config = ConfigDict(extra="ignore")

    api_version: Literal["common.sudo.dev/v1"]
    kind: Literal["ResourceRef"]
    zone_id: ExistingZoneIdRefStr
    path: ZonePathStr
    version: str | None = Field(default=None, min_length=1)
    digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    media_type: str | None = Field(default=None, min_length=1)
    size_bytes: str | None = Field(default=None, pattern=DECIMAL_STRING_PATTERN)


class ZoneDeployment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    location: Literal["cloud", "private", "edge"]
    data_domain: Literal["office", "core", "general"] | None = None
    trust_domain: str = Field(min_length=1)
    region: str | None = Field(default=None, min_length=1)
    replication_policy_ref: ResourceRef | None = None


class Zone(BaseModel):
    """Product zone record (§4.4). ``zone_id``/origin trust domain/``created_by`` immutable."""

    model_config = ConfigDict(extra="ignore")

    api_version: Literal["auth.sudo.dev/v1"]
    kind: Literal["Zone"]
    zone_id: ExistingZoneIdRefStr
    display_name: str = Field(min_length=1)
    description: str | None = None
    status: Literal["active", "suspended", "deleting", "deleted"]
    deployment: ZoneDeployment
    labels: dict[str, str] | None = None
    revision: str = Field(min_length=1)
    created_by: PrincipalRef
    created_at: str = Field(pattern=RFC3339_PATTERN)
    updated_at: str = Field(pattern=RFC3339_PATTERN)
    deleted_at: str | None = Field(default=None, pattern=RFC3339_PATTERN)


class ZoneCreateRequest(BaseModel):
    """Admission request (§4.2): zone_id is a strict tenant-create candidate."""

    model_config = ConfigDict(extra="ignore")

    api_version: Literal["auth.sudo.dev/v1"]
    kind: Literal["ZoneCreateRequest"]
    zone_id: TenantZoneIdCreateStr
    display_name: str = Field(min_length=1)
    description: str | None = None
    deployment: ZoneDeployment | None = None
    labels: dict[str, str] | None = None


class ZonePatchPlacement(BaseModel):
    """Whitelisted placement keys only; trust_domain is immutable and absent."""

    model_config = ConfigDict(extra="forbid")

    region: str | None = Field(default=None, min_length=1)
    data_domain: Literal["office", "core", "general"] | None = None


class ZonePatchRequest(BaseModel):
    """Whitelisted-field patch (§6.2) — deliberately not Partial[Zone].

    Unknown keys are rejected rather than ignored: naming anything outside
    the whitelist is a client bug, not a forward-compatibility case.
    """

    model_config = ConfigDict(extra="forbid")

    api_version: Literal["auth.sudo.dev/v1"]
    kind: Literal["ZonePatchRequest"]
    display_name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    labels: dict[str, str] | None = None
    deployment: ZonePatchPlacement | None = None


class ZoneGrantSource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_type: Literal["manual", "moss_org_binding", "migration", "system"]
    source_id: str = Field(min_length=1)


class ZoneGrant(BaseModel):
    """Auditable, revocable authorization fact (§4.6) — projections are downstream."""

    model_config = ConfigDict(extra="ignore")

    api_version: Literal["auth.sudo.dev/v1"]
    kind: Literal["ZoneGrant"]
    grant_id: str = Field(min_length=1)
    zone_id: ExistingZoneIdRefStr
    grantee: PrincipalRef
    capabilities: list[str] = Field(min_length=1)
    resource_prefixes: list[ZonePathStr] | None = None
    source: ZoneGrantSource | None = None
    issued_by: PrincipalRef
    reason: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    status: Literal["pending", "active", "revoked", "expired"]
    created_at: str = Field(pattern=RFC3339_PATTERN)
    not_before: str | None = Field(default=None, pattern=RFC3339_PATTERN)
    expires_at: str | None = Field(default=None, pattern=RFC3339_PATTERN)
    revoked_at: str | None = Field(default=None, pattern=RFC3339_PATTERN)
    revoked_by: PrincipalRef | None = None
    revoke_reason: str | None = Field(default=None, min_length=1)

    @property
    def has_unknown_capabilities(self) -> bool:
        return any(c not in KNOWN_CAPABILITIES for c in self.capabilities)


class ZoneGrantCreateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    api_version: Literal["auth.sudo.dev/v1"]
    kind: Literal["ZoneGrantCreateRequest"]
    grantee: PrincipalRef
    capabilities: list[str] = Field(min_length=1)
    resource_prefixes: list[ZonePathStr] | None = None
    source: ZoneGrantSource | None = None
    reason: str = Field(min_length=1)
    policy_version: str | None = Field(default=None, min_length=1)
    not_before: str | None = Field(default=None, pattern=RFC3339_PATTERN)
    expires_at: str | None = Field(default=None, pattern=RFC3339_PATTERN)


class ErrorInfo(BaseModel):
    """Inline error shape (§4.10); clients judge by code/retryable, never message."""

    model_config = ConfigDict(extra="ignore")

    code: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_]*$")
    message: str
    retryable: bool
    details: dict[str, object] | None = None
    cause_ref: ResourceRef | None = None
    request_id: str | None = Field(default=None, min_length=1)

    @property
    def is_unknown_code(self) -> bool:
        return self.code not in KNOWN_ERROR_CODES


class ZoneOperation(BaseModel):
    """Async operation (§4.9). Timeouts move the caller to unknown-and-poll."""

    model_config = ConfigDict(extra="ignore")

    api_version: Literal["auth.sudo.dev/v1"]
    kind: Literal["ZoneOperation"]
    operation_id: str = Field(min_length=1)
    action: Literal[
        "create",
        "patch",
        "suspend",
        "resume",
        "grant",
        "revoke",
        "mount",
        "unmount",
        "transfer",
        "deprovision",
    ]
    zone_id: str | None = Field(default=None, min_length=1)
    grant_id: str | None = Field(default=None, min_length=1)
    state: Literal["queued", "running", "waiting_dependency", "succeeded", "failed"]
    step: str = Field(min_length=1)
    retryable: bool
    revision: str | None = Field(default=None, min_length=1)
    result: dict[str, object] | None = None
    error: ErrorInfo | None = None
    created_at: str = Field(pattern=RFC3339_PATTERN)
    updated_at: str = Field(pattern=RFC3339_PATTERN)
    completed_at: str | None = Field(default=None, pattern=RFC3339_PATTERN)


#: wire kind -> model, so generic boundaries can dispatch on ``kind``.
ZONE_V1_MODELS: dict[str, type[BaseModel]] = {
    "PrincipalRef": PrincipalRef,
    "ResourceRef": ResourceRef,
    "Zone": Zone,
    "ZoneCreateRequest": ZoneCreateRequest,
    "ZonePatchRequest": ZonePatchRequest,
    "ZoneGrant": ZoneGrant,
    "ZoneGrantCreateRequest": ZoneGrantCreateRequest,
    "ZoneOperation": ZoneOperation,
}
