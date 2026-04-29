"""ORM mapping smoke tests."""

from nexus.bricks.approvals.db_models import (
    ApprovalDecisionModel,
    ApprovalRequestModel,
    ApprovalSessionAllowModel,
)


def test_table_names():
    assert ApprovalRequestModel.__tablename__ == "approval_requests"
    assert ApprovalDecisionModel.__tablename__ == "approval_decisions"
    assert ApprovalSessionAllowModel.__tablename__ == "approval_session_allow"


def test_request_columns_complete():
    cols = {c.name for c in ApprovalRequestModel.__table__.columns}
    assert {
        "id",
        "zone_id",
        "kind",
        "subject",
        "agent_id",
        "token_id",
        "session_id",
        "reason",
        "metadata",
        "status",
        "created_at",
        "decided_at",
        "decided_by",
        "decision_scope",
        "expires_at",
    } <= cols


def test_decision_columns_complete():
    cols = {c.name for c in ApprovalDecisionModel.__table__.columns}
    assert {
        "id",
        "request_id",
        "decided_at",
        "decided_by",
        "decision",
        "scope",
        "reason",
        "source",
    } <= cols


def test_session_allow_unique():
    constraints = {c.name for c in ApprovalSessionAllowModel.__table__.constraints}
    assert "uq_approval_session_allow" in constraints
