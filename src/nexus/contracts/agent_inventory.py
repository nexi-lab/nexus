"""Product Registry inventory and governance contracts; registration is not execution authority."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from nexus.contracts.product_agent_registry import (
    API,
    Digest,
    PrincipalRef,
    Revision,
    Stamp,
    Text,
    WireModel,
    canonical_bytes,
)

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,149}$")]
Label = Annotated[str, Field(min_length=1, max_length=200)]
Level = Literal["L0", "L1", "L2", "L3"]


class DataConnection(WireModel):
    reference: Annotated[str, Field(min_length=1, max_length=500)]
    classification: Literal["public", "internal", "confidential", "core"]
    actions: Annotated[
        list[Literal["read", "write", "delete", "export"]], Field(min_length=1, max_length=4)
    ]


class AssetMetadata(WireModel):
    name: Label
    source_platform: Literal[
        "dify", "langgraph", "openai", "mcp", "a2a", "http", "native", "saas", "other"
    ]
    source_instance: Annotated[str, Field(min_length=1, max_length=300)]
    source_agent_id: Identifier
    # Unknown discovery ownership is represented honestly; registration requires a member.
    owner_user_id: Identifier | None
    department_id: Identifier | None = None
    purpose: Text
    domain: Literal["office", "core"]
    hosting: Literal["external_hosted", "internal_hosted", "public_saas"]
    requested_level: Level
    data_connections: Annotated[list[DataConnection], Field(max_length=64)] = Field(
        default_factory=list
    )
    tool_refs: Annotated[list[Label], Field(max_length=64)] = Field(default_factory=list)
    model_paths: Annotated[list[Label], Field(max_length=32)] = Field(default_factory=list)
    risk_note: Annotated[str, Field(max_length=2000)] = ""

    @model_validator(mode="after")
    def public_saas_is_inventory_only(self):
        if self.hosting == "public_saas" and self.requested_level != "L0":
            raise ValueError("PUBLIC_SAAS_REGISTRATION_ONLY")
        return self


class DiscoveryEvidence(WireModel):
    source: Literal["router", "im", "platform", "catalog"]
    source_id: Identifier
    record_id: Identifier
    snapshot_digest: Digest
    observed_at: Stamp
    external_domain: bool = False


class InventoryCommand(WireModel):
    api_version: Literal[API]
    request_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")]
    asset_id: Identifier
    expected_revision: Revision
    # This is an assertion by the authenticated management gateway, not an IAM grant.
    initiated_by: Identifier


class SaveAsset(InventoryCommand):
    kind: Literal["SaveAsset"]
    metadata: AssetMetadata

    @model_validator(mode="after")
    def requires_owner(self):
        if not self.metadata.owner_user_id:
            raise ValueError("AGENT_ASSET_OWNER_REQUIRED")
        return self


class ObserveAsset(InventoryCommand):
    kind: Literal["ObserveAsset"]
    metadata: AssetMetadata
    observation: DiscoveryEvidence


class ClaimAsset(InventoryCommand):
    kind: Literal["ClaimAsset"]
    owner_user_id: Identifier
    department_id: Identifier | None = None


class SubmitAsset(InventoryCommand):
    kind: Literal["SubmitAsset"]


class ReviewAsset(InventoryCommand):
    kind: Literal["ReviewAsset"]
    decision: Literal["approve", "return"]
    reason: Text


class SuspendAsset(InventoryCommand):
    kind: Literal["SuspendAsset"]
    reason: Text


class LinkAsset(InventoryCommand):
    kind: Literal["LinkAsset"]
    principal: PrincipalRef


class MergeAsset(InventoryCommand):
    kind: Literal["MergeAsset"]
    target_asset_id: Identifier
    target_revision: Revision
    reason: Text


class AcceptanceReport(WireModel):
    kind: Literal["AgentAcceptanceReport"] = "AgentAcceptanceReport"
    asset_id: Identifier
    metadata_digest: Digest
    spec_digest: Digest
    level: Literal["L1", "L2", "L3"]
    expires_at: Stamp
    execution_revision: Digest | None = None
    evidence_refs: Annotated[list[Digest], Field(min_length=1, max_length=32)]
    checks: Annotated[
        list[
            Literal[
                "entry",
                "identity",
                "router",
                "revoke",
                "tool",
                "isolation",
                "evaluation",
                "trace",
                "native",
            ]
        ],
        Field(min_length=1, max_length=9),
    ]
    evaluation_id: Identifier | None = None
    dataset_digest: Digest | None = None
    rubric_digest: Digest | None = None
    before_score: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None = None
    after_score: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None = None
    allowed_drop: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None = None


class RecordAcceptance(InventoryCommand):
    kind: Literal["RecordAcceptance"]
    report: AcceptanceReport


AssetCommand = Annotated[
    SaveAsset
    | ObserveAsset
    | ClaimAsset
    | SubmitAsset
    | ReviewAsset
    | SuspendAsset
    | LinkAsset
    | MergeAsset
    | RecordAcceptance,
    Field(discriminator="kind"),
]
ASSET_COMMAND = TypeAdapter(AssetCommand)


class AssetRecord(WireModel):
    api_version: Literal[API] = API
    kind: Literal["AgentAsset"] = "AgentAsset"
    id: Identifier
    owner_zone: str
    metadata: AssetMetadata
    metadata_digest: Digest
    revision: Revision
    status: Literal[
        "discovered", "draft", "submitted", "returned", "approved", "suspended", "merged"
    ]
    actual_level: Level
    recommended_level: Level
    recommendation_reasons: list[str]
    risk: Literal["low", "medium", "high", "external"]
    external_domain: bool
    effective_domain: Literal["office", "core"] = "office"
    principal: PrincipalRef | None = None
    merged_into: Identifier | None = None
    observations: list[DiscoveryEvidence]
    review_reason: str | None = None
    acceptance: AcceptanceReport | None = None
    acceptance_issuer: str | None = None
    evidence_status: Literal["none", "current", "expired", "untrusted", "blocked"] = "none"
    created_at: Stamp
    updated_at: Stamp
    # Never synthesize a zero-cost total when the trusted Router ledger is absent.
    cost_status: Literal["unavailable", "router_verified"] = "unavailable"


class AssetPage(WireModel):
    api_version: Literal[API] = API
    kind: Literal["AgentAssetPage"] = "AgentAssetPage"
    items: list[AssetRecord]
    next_cursor: str | None


class AssetEvent(WireModel):
    revision: Revision
    operation: str
    actor: str
    initiated_by: str
    created_at: Stamp
    reason: str | None


class AssetHistory(WireModel):
    api_version: Literal[API] = API
    kind: Literal["AgentAssetHistory"] = "AgentAssetHistory"
    asset_id: Identifier
    events: list[AssetEvent]
    next_revision: Revision | None


def parse_asset_command(data: bytes) -> AssetCommand:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("DUPLICATE_KEY")
            result[key] = value
        return result

    if len(data) > 65536:
        raise ValueError("BODY_LIMIT")
    value = json.loads(data, object_pairs_hook=unique)
    canonical_bytes(value)
    return ASSET_COMMAND.validate_python(value)
