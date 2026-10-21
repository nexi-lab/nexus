"""Task-bound IAM delegation profile. No new token format or signing authority."""

from typing import Annotated, Literal

from pydantic import Field, SecretStr, model_validator

from nexus.contracts.agent_inventory import Identifier, Label
from nexus.contracts.product_agent_registry import API, Digest, Stamp, Version, WireModel


class ExecutionPermission(WireModel):
    resource: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,499}$")]
    operation: Literal[
        "model.invoke", "tool.invoke", "data.read", "data.write", "data.delete", "data.export"
    ]


class IssueExecutionGrant(WireModel):
    api_version: Literal[API] = API
    kind: Literal["IssueExecutionGrant"] = "IssueExecutionGrant"
    request_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")]
    asset_id: Identifier
    metadata_digest: Digest
    spec_digest: Digest
    task_id: Identifier
    # Human provenance asserted by the authenticated gateway; NOT IAM delegation.
    initiated_by: Identifier
    mode: Literal["run", "evaluation"]
    version: Version | None = None
    dataset_digest: Digest | None = None
    permissions: Annotated[list[ExecutionPermission], Field(min_length=1, max_length=64)]
    ttl_seconds: Annotated[int, Field(ge=1, le=300)] = 120
    max_model_calls: Annotated[int, Field(ge=1, le=32)] = 8

    @model_validator(mode="after")
    def selectors(self):
        if self.mode == "run" and (not self.version or self.dataset_digest is not None):
            raise ValueError("EXECUTION_RUN_VERSION_REQUIRED")
        if self.mode == "evaluation" and (self.version is not None or not self.dataset_digest):
            raise ValueError("EXECUTION_EVALUATION_DATASET_REQUIRED")
        if len({(p.operation, p.resource) for p in self.permissions}) != len(self.permissions):
            raise ValueError("EXECUTION_DUPLICATE_PERMISSION")
        return self


class ExecutionGrant(WireModel):
    api_version: Literal[API] = API
    kind: Literal["AgentExecutionGrant"] = "AgentExecutionGrant"
    id: Identifier
    zone: Identifier
    issuer: Label
    request: IssueExecutionGrant
    worker_id: Identifier
    delegation_id: str | None = None
    key_id: str | None = None
    owner_user_id: Identifier
    department_id: Identifier | None
    status: Literal["issuing", "active", "revoked", "failed"]
    cleanup_pending: bool
    created_at: Stamp
    expires_at: Stamp
    reason: str | None = None
    stopped_by: Label | None = None
    stopped_at: Stamp | None = None


class ExecutionGrantPage(WireModel):
    kind: Literal["ExecutionGrantPage"] = "ExecutionGrantPage"
    items: list[ExecutionGrant]
    next_cursor: Identifier | None


class ExecutionGrantResult(WireModel):
    kind: Literal["ExecutionGrantResult"] = "ExecutionGrantResult"
    grant: ExecutionGrant
    # Returned ONCE; never persisted by the Registry or returned by GET/replay.
    api_key: str | None = Field(default=None, repr=False)


class CheckExecutionGrant(WireModel):
    api_version: Literal[API] = API
    kind: Literal["CheckExecutionGrant"] = "CheckExecutionGrant"
    grant_id: Identifier
    task_id: Identifier
    spec_digest: Digest
    permission: ExecutionPermission
    token: SecretStr


class ExecutionAuthorization(WireModel):
    kind: Literal["ExecutionAuthorization"] = "ExecutionAuthorization"
    grant: ExecutionGrant
    authorized_at: Stamp
    # Point-in-time decision; gateways MUST NOT cache/reuse this for another call.
    cacheable: Literal[False] = False


class ExecutionZonePolicy(WireModel):
    max_model_calls: Annotated[int, Field(ge=1, le=32)] = 8
    coordinator_id: Identifier
    coordinator_owner_id: Identifier
    issuers: Annotated[list[Label], Field(min_length=1, max_length=32)]
    evaluators: Annotated[list[Label], Field(max_length=32)] = Field(default_factory=list)
    gateways: Annotated[list[Label], Field(min_length=1, max_length=32)]
    permissions: Annotated[list[ExecutionPermission], Field(max_length=128)]
    evaluation_permissions: Annotated[list[ExecutionPermission], Field(max_length=128)] = Field(
        default_factory=list
    )
