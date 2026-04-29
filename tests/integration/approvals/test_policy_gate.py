"""PolicyGate integration tests."""

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


@pytest.mark.asyncio
async def test_session_allow_cache_short_circuits(approval_service):
    gate = PolicyGate(approval_service)
    tag = _tag()
    sid = f"tok:s:{tag}"
    zone = f"z_{tag}"
    subject = f"cache.example:443:{tag}"

    # First call: approve at session scope to seed the cache.
    waiter = asyncio.create_task(
        gate.check(
            kind=ApprovalKind.EGRESS_HOST,
            subject=subject,
            zone_id=zone,
            token_id="tok",
            session_id=sid,
            agent_id="ag",
            reason="r",
            metadata={},
        )
    )
    # Wait for the pending row to land.
    rid: str | None = None
    for _ in range(50):
        await asyncio.sleep(0.1)
        pending = await approval_service.list_pending(zone_id=zone)
        match = [p for p in pending if p.subject == subject]
        if match:
            rid = match[0].id
            break
    assert rid is not None
    await approval_service.decide(
        request_id=rid,
        decision=Decision.APPROVED,
        decided_by="op",
        scope=DecisionScope.SESSION,
        reason=None,
        source=DecisionSource.GRPC,
    )
    assert (await asyncio.wait_for(waiter, 5.0)) is Decision.APPROVED

    # Second call (same session/zone/kind/subject): cache hit, no new pending row.
    fast = await asyncio.wait_for(
        gate.check(
            kind=ApprovalKind.EGRESS_HOST,
            subject=subject,
            zone_id=zone,
            token_id="tok",
            session_id=sid,
            agent_id="ag",
            reason="r",
            metadata={},
        ),
        timeout=0.5,
    )
    assert fast is Decision.APPROVED
    pending2 = await approval_service.list_pending(zone_id=zone)
    assert all(p.subject != subject for p in pending2)


@pytest.mark.asyncio
async def test_deny_returns_decision_denied(approval_service):
    gate = PolicyGate(approval_service)
    tag = _tag()
    zone = f"z_{tag}"
    subject = f"reject.example:443:{tag}"

    waiter = asyncio.create_task(
        gate.check(
            kind=ApprovalKind.EGRESS_HOST,
            subject=subject,
            zone_id=zone,
            token_id="tok",
            session_id=f"tok:s:{tag}",
            agent_id="ag",
            reason="r",
            metadata={},
        )
    )
    rid: str | None = None
    for _ in range(50):
        await asyncio.sleep(0.1)
        pending = await approval_service.list_pending(zone_id=zone)
        match = [p for p in pending if p.subject == subject]
        if match:
            rid = match[0].id
            break
    assert rid is not None
    await approval_service.decide(
        request_id=rid,
        decision=Decision.DENIED,
        decided_by="op",
        scope=DecisionScope.ONCE,
        reason="nope",
        source=DecisionSource.GRPC,
    )
    decision = await asyncio.wait_for(waiter, 5.0)
    assert decision is Decision.DENIED


@pytest.mark.asyncio
async def test_timeout_returns_decision_denied(approval_service_short):
    """approval_service_short uses auto_deny_after_seconds=0.2."""
    gate = PolicyGate(approval_service_short)
    tag = _tag()

    decision = await gate.check(
        kind=ApprovalKind.EGRESS_HOST,
        subject=f"slow.example:443:{tag}",
        zone_id=f"z_{tag}",
        token_id="tok",
        session_id=f"tok:s:{tag}",
        agent_id="ag",
        reason="r",
        metadata={},
    )
    assert decision is Decision.DENIED
