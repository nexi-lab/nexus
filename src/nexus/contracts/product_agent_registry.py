"""Experimental product publication contracts; independent of runtime PID identity."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, WithJsonSchema

from nexus.contracts.resource_ref import load_resource_ref_schema, validate_resource_ref

API = "agent-registry.sudo.dev/v1alpha1"
MAX_BODY_BYTES = 65536
Domain = Annotated[
    str,
    Field(
        pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$",
        max_length=253,
    ),
]
Name = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")]
Version = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
Digest = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
Text = Annotated[str, Field(min_length=1, max_length=2000)]
Stamp = Annotated[int, Field(ge=0, le=9007199254740991)]
Revision = Annotated[int, Field(ge=0, le=9007199254740991)]


def _ref(value: dict[str, Any]) -> dict[str, Any]:
    if validate_resource_ref(value):
        raise ValueError("INVALID_RESOURCE_REF")
    return value


ResourceReference = Annotated[
    dict[str, Any],
    AfterValidator(_ref),
    # Pydantic cannot export external URN refs. The owner exporter resolves this marker
    # into the existing canonical ResourceRef reference; runtime uses its adapter above.
    WithJsonSchema({"type": "object", "x-sudo-resource-ref": load_resource_ref_schema()["$id"]}),
]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PrincipalRef(WireModel):
    trust_domain: Domain
    agent_name: Name


class Command(WireModel):
    api_version: Literal[API]
    request_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")]
    principal: PrincipalRef


class RegisterPrincipal(Command):
    kind: Literal["RegisterPrincipal"]
    display_name: Annotated[str, Field(min_length=1, max_length=200)]


class PublishVersion(Command):
    kind: Literal["PublishVersion"]
    version: Version
    spec_ref: ResourceReference
    spec_digest: Digest
    source_evidence_refs: Annotated[list[ResourceReference], Field(max_length=16)] = Field(
        default_factory=list
    )


class DecideVersion(Command):
    kind: Literal["DecideVersion"]
    version: Version
    decision: Literal["approved", "withdrawn", "revoked"]
    reason: Text
    expires_at: Stamp | None = None


class MoveChannel(Command):
    kind: Literal["MoveChannel"]
    channel: Literal["stable", "canary"]
    version: Version
    expected_revision: Revision
    reason: Text


class SetPrincipalStatus(Command):
    kind: Literal["SetPrincipalStatus"]
    status: Literal["active", "suspended", "revoked"]
    expected_revision: Revision
    reason: Text


RegistryCommand = Annotated[
    RegisterPrincipal | PublishVersion | DecideVersion | MoveChannel | SetPrincipalStatus,
    Field(discriminator="kind"),
]
COMMAND_ADAPTER = TypeAdapter(RegistryCommand)


class Principal(WireModel):
    principal: PrincipalRef
    owner_zone: str
    display_name: str
    status: Literal["active", "suspended", "revoked"]
    revision: Revision
    created_at: Stamp


class VersionManifest(WireModel):
    api_version: Literal[API] = API
    kind: Literal["VersionManifest"] = "VersionManifest"
    principal: PrincipalRef
    version: Version
    spec_ref: ResourceReference
    spec_digest: Digest
    source_evidence_refs: list[ResourceReference]
    created_by: str
    created_at: Stamp
    evidence: Literal["signed_registration_only"] = "signed_registration_only"


class SignedVersion(WireModel):
    manifest: VersionManifest
    payload_base64: str
    digest: Digest
    algorithm: Literal["Ed25519"] = "Ed25519"
    key_id: Digest
    signature_base64: str


class Decision(WireModel):
    id: str
    version: Version
    decision: Literal["approved", "withdrawn", "revoked"]
    actor: str
    reason: str
    expires_at: Stamp | None
    created_at: Stamp
    registry_revision: Revision


class Channel(WireModel):
    name: Literal["stable", "canary"]
    version: Version
    spec_digest: Digest
    revision: Revision
    enabled: bool
    updated_at: Stamp


class CommandResult(WireModel):
    api_version: Literal[API] = API
    kind: Literal["RegistryCommandResult"] = "RegistryCommandResult"
    principal: Principal
    version: SignedVersion | None = None
    decision: Decision | None = None
    channel: Channel | None = None


class VersionItem(WireModel):
    version: SignedVersion
    decision: Decision | None


class VersionsPage(WireModel):
    api_version: Literal[API] = API
    kind: Literal["RegistryVersionsPage"] = "RegistryVersionsPage"
    principal: Principal
    items: list[VersionItem]
    channels: list[Channel]
    next_cursor: str | None


class Resolution(WireModel):
    api_version: Literal[API] = API
    kind: Literal["RegistryResolution"] = "RegistryResolution"
    principal: Principal
    version: SignedVersion
    approval: Decision
    channel: Channel | None
    evaluated_at: Stamp


def canonical_bytes(value: Any) -> bytes:
    """Signing is over the returned UTF-8 payload bytes, not client reserialization."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def parse_command(data: bytes) -> RegistryCommand:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("DUPLICATE_KEY")
            result[key] = value
        return result

    if len(data) > MAX_BODY_BYTES:
        raise ValueError("BODY_LIMIT")
    value = json.loads(data, object_pairs_hook=unique)
    # Reject non-JSON numbers and lone surrogate strings before hashing or persistence.
    canonical_bytes(value)
    return COMMAND_ADAPTER.validate_python(value)
