"""Gate-contract integration test for the MCP egress hook (Task 18, #3790).

This test exercises ``PolicyGate.check`` directly against a live
``approval_service``. It is the contract that the MCP egress middleware
will call when it encounters an unlisted host: a pending row should be
created with the supplied ``subject``, ``kind``, and ``metadata``, and
once an operator decides ``Decision.APPROVED`` the gate returns
``Decision.APPROVED``.

The full middleware-end-to-end test runs in Task 21 — this file only
locks the gate-call contract.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from nexus.bricks.approvals.models import (
    ApprovalKind,
    Decision,
    DecisionScope,
    DecisionSource,
)
from nexus.bricks.approvals.policy_gate import PolicyGate

pytestmark = pytest.mark.integration


def _tag() -> str:
    return uuid.uuid4().hex[:12]


async def _wait_pending_by_subject(service, zone_id: str, subject: str):
    """Poll list_pending(zone) for a row whose subject matches.

    PolicyGate.check generates its own request_id internally, so callers
    that only know (zone, subject) must poll by-subject. Mirrors the
    pattern used in test_policy_gate.py.
    """
    for _ in range(50):
        await asyncio.sleep(0.1)
        pending = await service.list_pending(zone_id=zone_id)
        match = [p for p in pending if p.subject == subject]
        if match:
            return match[0]
    raise AssertionError(f"pending row for subject {subject!r} never landed in DB")


@pytest.mark.asyncio
async def test_unlisted_egress_creates_pending_request(approval_service):
    """The middleware hook creates a pending row for an unlisted host
    and unblocks on approve.

    This locks the contract that MCP egress middleware will rely on:
      - ``subject`` round-trips verbatim ("host:port" form).
      - ``kind`` is preserved as ``EGRESS_HOST``.
      - ``metadata`` round-trips verbatim (URL + tool name).
      - An operator approve unblocks the gate with ``Decision.APPROVED``.
    """
    gate = PolicyGate(approval_service)
    tag = _tag()
    zone = f"z_{tag}"
    subject = f"api.stripe.com:443:{tag}"
    url = f"https://api.stripe.com/v1/charges?tag={tag}"

    async def caller():
        return await gate.check(
            kind=ApprovalKind.EGRESS_HOST,
            subject=subject,
            zone_id=zone,
            token_id=f"tok_{tag}",
            session_id=f"tok:s:{tag}",
            agent_id=f"ag_{tag}",
            reason="nexus_fetch",
            metadata={"url": url, "tool": "nexus_fetch"},
        )

    waiter = asyncio.create_task(caller())

    pending = await _wait_pending_by_subject(approval_service, zone, subject)

    # Contract assertions — what the middleware will rely on.
    assert pending.kind is ApprovalKind.EGRESS_HOST
    assert pending.subject == subject
    assert pending.metadata.get("url") == url
    assert pending.metadata.get("tool") == "nexus_fetch"
    assert pending.zone_id == zone
    assert pending.token_id == f"tok_{tag}"
    assert pending.session_id == f"tok:s:{tag}"
    assert pending.agent_id == f"ag_{tag}"
    assert pending.reason == "nexus_fetch"

    await approval_service.decide(
        request_id=pending.id,
        decision=Decision.APPROVED,
        decided_by="op",
        scope=DecisionScope.SESSION,
        reason=None,
        source=DecisionSource.GRPC,
    )
    assert (await asyncio.wait_for(waiter, 5.0)) is Decision.APPROVED


@pytest.mark.asyncio
async def test_unlisted_egress_denied_returns_decision_denied(approval_service):
    """An operator-deny on an egress request returns Decision.DENIED — the
    middleware will then keep its existing deny path.
    """
    gate = PolicyGate(approval_service)
    tag = _tag()
    zone = f"z_{tag}"
    subject = f"blocked.example.com:443:{tag}"

    async def caller():
        return await gate.check(
            kind=ApprovalKind.EGRESS_HOST,
            subject=subject,
            zone_id=zone,
            token_id=f"tok_{tag}",
            session_id=f"tok:s:{tag}",
            agent_id=f"ag_{tag}",
            reason="nexus_fetch",
            metadata={"url": f"https://blocked.example.com/x?tag={tag}"},
        )

    waiter = asyncio.create_task(caller())

    pending = await _wait_pending_by_subject(approval_service, zone, subject)
    assert pending.kind is ApprovalKind.EGRESS_HOST

    await approval_service.decide(
        request_id=pending.id,
        decision=Decision.DENIED,
        decided_by="op",
        scope=DecisionScope.ONCE,
        reason="not allowed",
        source=DecisionSource.GRPC,
    )
    assert (await asyncio.wait_for(waiter, 5.0)) is Decision.DENIED
