"""Domain models for approval requests + decisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class ApprovalRequestStatus(StrEnum):
    """Lifecycle status of an approval request row."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalKind(StrEnum):
    EGRESS_HOST = "egress_host"
    MCP_TOOL = "mcp_tool"
    ZONE_ACCESS = "zone_access"
    PACKAGE_INSTALL = "package_install"


class DecisionScope(StrEnum):
    ONCE = "once"
    SESSION = "session"
    PERSIST_SANDBOX = "persist_sandbox"
    PERSIST_BASELINE = "persist_baseline"


class Decision(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"


class DecisionSource(StrEnum):
    GRPC = "grpc"
    HTTP = "http"
    SYSTEM_TIMEOUT = "system_timeout"
    PUSH_API = "push_api"


@dataclass(frozen=True)
class ApprovalRequest:
    """Domain representation of one row in approval_requests."""

    id: str
    zone_id: str
    kind: ApprovalKind
    subject: str
    agent_id: str | None
    token_id: str | None
    session_id: str | None
    reason: str
    metadata: dict[str, Any]
    status: ApprovalRequestStatus
    created_at: datetime
    decided_at: datetime | None
    decided_by: str | None
    decision_scope: DecisionScope | None
    expires_at: datetime

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["status"] = self.status.value
        d["decision_scope"] = self.decision_scope.value if self.decision_scope else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ApprovalRequest:
        return cls(
            id=d["id"],
            zone_id=d["zone_id"],
            kind=ApprovalKind(d["kind"]),
            subject=d["subject"],
            agent_id=d["agent_id"],
            token_id=d["token_id"],
            session_id=d["session_id"],
            reason=d["reason"],
            metadata=d["metadata"],
            status=ApprovalRequestStatus(d["status"]),
            created_at=d["created_at"],
            decided_at=d["decided_at"],
            decided_by=d["decided_by"],
            decision_scope=DecisionScope(d["decision_scope"]) if d["decision_scope"] else None,
            expires_at=d["expires_at"],
        )
