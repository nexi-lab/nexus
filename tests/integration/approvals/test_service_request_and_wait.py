"""ApprovalService.request_and_wait integration tests."""

import asyncio
import uuid

import pytest

from nexus.bricks.approvals.config import ApprovalConfig
from nexus.bricks.approvals.errors import ApprovalDenied, ApprovalTimeout
from nexus.bricks.approvals.events import NotifyBridge
from nexus.bricks.approvals.models import (
    ApprovalKind,
    ApprovalRequestStatus,
    Decision,
    DecisionScope,
    DecisionSource,
)
from nexus.bricks.approvals.repository import ApprovalRepository
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
async def test_session_scope_fans_out_session_allow_to_every_coalesced_waiter(
    approval_service: ApprovalService,
):
    """Issue #3790 follow-up regression: when N waiters coalesce on
    (zone, kind, subject) with N different session_ids, a SESSION-scope
    approval must insert ``session_allow`` for *every* registered
    session_id — not just the winning insert's. Otherwise the losers
    create a fresh pending row on the next same-session call.
    """
    tag = _tag()
    zone = f"z_session_{tag}"
    subject = f"shared.session.example:443:{tag}"
    kind = ApprovalKind.EGRESS_HOST
    sids = [f"tok:s_{i}_{tag}" for i in range(3)]

    async def call(rid: str, sid: str) -> Decision:
        return await approval_service.request_and_wait(
            request_id=rid,
            zone_id=zone,
            kind=kind,
            subject=subject,
            agent_id="ag",
            token_id="tok",
            session_id=sid,
            reason="r",
            metadata={},
        )

    tasks = [asyncio.create_task(call(f"req_se_{i}_{tag}", sids[i])) for i in range(3)]

    # Wait for the coalesced row to appear.
    coalesced_id: str | None = None
    for _ in range(50):
        await asyncio.sleep(0.1)
        pending = await approval_service.list_pending(zone_id=zone)
        rows = [p for p in pending if p.subject == subject]
        if len(rows) == 1 and approval_service._dispatcher.waiter_count(rows[0].id) >= 3:
            coalesced_id = rows[0].id
            break
    else:
        raise AssertionError("3 coalesced waiters never registered against the same row")

    await approval_service.decide(
        request_id=coalesced_id,
        decision=Decision.APPROVED,
        decided_by="op",
        scope=DecisionScope.SESSION,
        reason=None,
        source=DecisionSource.GRPC,
    )

    for task in tasks:
        assert await asyncio.wait_for(task, 5.0) is Decision.APPROVED

    # Every waiter's session_id must have an allow row.
    for sid in sids:
        assert await approval_service.repository.session_allow_exists(
            session_id=sid,
            zone_id=zone,
            kind=kind,
            subject=subject,
        ), f"missing session_allow for {sid}"


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


@pytest.mark.asyncio
async def test_short_timeout_waiter_cannot_expire_shared_row(session_factory, asyncpg_pool) -> None:
    """Round-4 (#3790): a coalesced waiter with a shorter timeout_override
    must NOT expire the shared DB row while the row's own expires_at is
    still in the future.

    Before the fix, the timeout path called transition(EXPIRED) blindly
    using the local ``auto_deny_after_seconds`` limit. That could write
    EXPIRED on a row whose stored ``expires_at`` was far in the future,
    converting an approvable request to a denial for the remaining waiters.
    """
    repo = ApprovalRepository(session_factory)
    # Long row-level timeout so the shared row stays approvable after the
    # short waiter gives up.
    svc = ApprovalService(
        repo,
        NotifyBridge(asyncpg_pool),
        ApprovalConfig(enabled=True, auto_deny_after_seconds=30.0),
    )
    await svc.start()
    try:
        tag = _tag()
        zone = f"z_short_{tag}"
        subject = f"short.example:443:{tag}"

        # Waiter A uses a tiny timeout_override so it gives up quickly.
        waiter_a = asyncio.create_task(
            svc.request_and_wait(
                request_id=f"req_short_a_{tag}",
                zone_id=zone,
                kind=ApprovalKind.EGRESS_HOST,
                subject=subject,
                agent_id="ag",
                token_id="tok",
                session_id=f"tok:s_a:{tag}",
                reason="r",
                metadata={},
                timeout_override=0.2,
            )
        )

        # Wait for the shared pending row.
        coalesced_id: str | None = None
        for _ in range(50):
            await asyncio.sleep(0.1)
            rows = [r for r in await svc.list_pending(zone_id=zone) if r.subject == subject]
            if rows:
                coalesced_id = rows[0].id
                break
        else:
            raise AssertionError("shared pending row never appeared")

        # Waiter B coalesces onto the same row with the full row timeout.
        waiter_b = asyncio.create_task(
            svc.request_and_wait(
                request_id=f"req_short_b_{tag}",
                zone_id=zone,
                kind=ApprovalKind.EGRESS_HOST,
                subject=subject,
                agent_id="ag",
                token_id="tok",
                session_id=f"tok:s_b:{tag}",
                reason="r",
                metadata={},
            )
        )

        # Waiter A times out — must NOT expire the shared row.
        with pytest.raises(ApprovalTimeout):
            await asyncio.wait_for(waiter_a, 2.0)

        # The shared row must still be PENDING (not expired).
        row = await svc.get(coalesced_id)
        assert row is not None
        assert row.status is ApprovalRequestStatus.PENDING, (
            f"short-timeout waiter prematurely expired the shared row: {row.status}"
        )

        # Now approve — waiter B should still resolve.
        await svc.decide(
            request_id=coalesced_id,
            decision=Decision.APPROVED,
            decided_by="op",
            scope=DecisionScope.ONCE,
            reason=None,
            source=DecisionSource.GRPC,
        )
        assert await asyncio.wait_for(waiter_b, 5.0) is Decision.APPROVED
    finally:
        await svc.stop()
