"""ApprovalService.request_and_wait integration tests."""

import asyncio
import uuid

import pytest

from nexus.bricks.approvals.errors import ApprovalDenied, ApprovalTimeout
from nexus.bricks.approvals.models import (
    ApprovalKind,
    Decision,
    DecisionScope,
    DecisionSource,
)
from nexus.bricks.approvals.service import ApprovalService

pytestmark = pytest.mark.integration


def _tag() -> str:
    return uuid.uuid4().hex[:12]


async def _wait_pending(service: ApprovalService, rid: str) -> None:
    """Poll until the pending row is durably committed.

    Fixed sleeps race under xdist parallel load — the request_and_wait
    insert may not have landed yet when the test moves on.
    """
    for _ in range(50):
        await asyncio.sleep(0.1)
        if (await service.get(rid)) is not None:
            return
    raise AssertionError(f"pending row {rid} never landed in DB")


@pytest.mark.asyncio
async def test_approve_unblocks_waiting_caller(approval_service: ApprovalService):
    tag = _tag()
    rid = f"req_a_{tag}"
    waiting = asyncio.create_task(
        approval_service.request_and_wait(
            request_id=rid,
            zone_id=f"z_{tag}",
            kind=ApprovalKind.EGRESS_HOST,
            subject=f"api.x:443:{tag}",
            agent_id="ag",
            token_id="tok",
            session_id=f"tok:s:{tag}",
            reason="r",
            metadata={},
        )
    )
    await _wait_pending(approval_service, rid)

    decided = await approval_service.decide(
        request_id=rid,
        decision=Decision.APPROVED,
        decided_by="op",
        scope=DecisionScope.ONCE,
        reason=None,
        source=DecisionSource.GRPC,
    )
    assert decided.status.value == "approved"
    assert (await asyncio.wait_for(waiting, 5.0)) is Decision.APPROVED


@pytest.mark.asyncio
async def test_deny_raises_approval_denied(approval_service: ApprovalService):
    tag = _tag()
    rid = f"req_b_{tag}"
    waiting = asyncio.create_task(
        approval_service.request_and_wait(
            request_id=rid,
            zone_id=f"z_{tag}",
            kind=ApprovalKind.EGRESS_HOST,
            subject=f"api.y:443:{tag}",
            agent_id="ag",
            token_id="tok",
            session_id=f"tok:s:{tag}",
            reason="r",
            metadata={},
        )
    )
    await _wait_pending(approval_service, rid)
    await approval_service.decide(
        request_id=rid,
        decision=Decision.DENIED,
        decided_by="op",
        scope=DecisionScope.ONCE,
        reason="nope",
        source=DecisionSource.GRPC,
    )
    with pytest.raises(ApprovalDenied):
        await asyncio.wait_for(waiting, 5.0)


@pytest.mark.asyncio
async def test_timeout_raises_approval_timeout(approval_service_short: ApprovalService):
    """approval_service_short fixture sets auto_deny_after_seconds=0.2."""
    tag = _tag()
    with pytest.raises(ApprovalTimeout):
        await approval_service_short.request_and_wait(
            request_id=f"req_c_{tag}",
            zone_id=f"z_{tag}",
            kind=ApprovalKind.EGRESS_HOST,
            subject=f"slow:443:{tag}",
            agent_id="ag",
            token_id="tok",
            session_id=f"tok:s:{tag}",
            reason="r",
            metadata={},
        )


@pytest.mark.asyncio
async def test_concurrent_callers_same_subject_share_one_row(approval_service: ApprovalService):
    tag = _tag()
    zone = f"z_{tag}"
    subject = f"shared.example:443:{tag}"

    async def call(rid: str):
        return await approval_service.request_and_wait(
            request_id=rid,
            zone_id=zone,
            kind=ApprovalKind.EGRESS_HOST,
            subject=subject,
            agent_id="ag",
            token_id="tok",
            session_id=f"tok:s_{rid}",
            reason="r",
            metadata={},
        )

    t1 = asyncio.create_task(call(f"req_d1_{tag}"))
    t2 = asyncio.create_task(call(f"req_d2_{tag}"))

    # Wait until exactly one coalesced row appears in the pending list.
    coalesced_id: str | None = None
    for _ in range(50):
        await asyncio.sleep(0.1)
        pending = await approval_service.list_pending(zone_id=zone)
        rows = [p for p in pending if p.subject == subject]
        if len(rows) == 1:
            coalesced_id = rows[0].id
            break
    else:
        raise AssertionError("coalesced row did not appear in pending list")

    await approval_service.decide(
        request_id=coalesced_id,
        decision=Decision.APPROVED,
        decided_by="op",
        scope=DecisionScope.ONCE,
        reason=None,
        source=DecisionSource.GRPC,
    )
    assert await asyncio.wait_for(t1, 5.0) is Decision.APPROVED
    assert await asyncio.wait_for(t2, 5.0) is Decision.APPROVED
