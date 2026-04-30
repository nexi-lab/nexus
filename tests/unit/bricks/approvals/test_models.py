"""Domain model tests for the approvals brick."""

from datetime import UTC, datetime, timedelta

import pytest

from nexus.bricks.approvals.models import (
    ApprovalKind,
    ApprovalRequest,
    ApprovalRequestStatus,
    Decision,
    DecisionScope,
    DecisionSource,
)


def test_approval_kind_has_all_four_values_from_issue():
    assert {k.value for k in ApprovalKind} == {
        "egress_host",
        "mcp_tool",
        "zone_access",
        "package_install",
    }


def test_decision_scope_has_all_four_values():
    assert {s.value for s in DecisionScope} == {
        "once",
        "session",
        "persist_sandbox",
        "persist_baseline",
    }


def test_decision_terminal_values():
    assert Decision.APPROVED.value == "approved"
    assert Decision.DENIED.value == "denied"


def test_decision_source_values():
    assert {s.value for s in DecisionSource} == {
        "grpc",
        "http",
        "system_timeout",
        "push_api",
        "system_inherited",
    }


def test_approval_request_round_trips_through_dict():
    now = datetime.now(UTC)
    req = ApprovalRequest(
        id="req_01HABC",
        zone_id="eng",
        kind=ApprovalKind.EGRESS_HOST,
        subject="api.stripe.com:443",
        agent_id="claude-1",
        token_id="tok_alice",
        session_id="tok_alice:sess_1",
        reason="nexus_fetch",
        metadata={"url": "https://api.stripe.com/v1/charges"},
        status=ApprovalRequestStatus.PENDING,
        created_at=now,
        decided_at=None,
        decided_by=None,
        decision_scope=None,
        expires_at=now + timedelta(seconds=60),
    )
    d = req.to_dict()
    again = ApprovalRequest.from_dict(d)
    assert again == req


def test_approval_request_status_must_be_enum_value():
    with pytest.raises(ValueError, match="weird"):
        ApprovalRequestStatus("weird")
