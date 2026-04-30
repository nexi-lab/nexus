"""ApprovalService.decide() resilience tests (Issue #3790, F3).

Once ``transition`` commits the new status, the row is non-pending and a
retry can't re-decide it. ``decide()`` MUST resolve local dispatcher
futures before any best-effort step (session_allow inserts, NOTIFY
publish), and best-effort steps must not propagate exceptions —
otherwise local waiters strand until timeout and cross-worker waiters
never get unblocked (the cross-worker case is recovered by
``reconcile_in_flight`` on listener reattach; see service.start()).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from nexus.bricks.approvals.models import (
    ApprovalKind,
    ApprovalRequestStatus,
    Decision,
    DecisionScope,
    DecisionSource,
)
from nexus.bricks.approvals.service import ApprovalService

pytestmark = pytest.mark.integration


def _tag() -> str:
    return uuid.uuid4().hex[:12]


async def _wait_pending(service: ApprovalService, rid: str) -> None:
    for _ in range(50):
        await asyncio.sleep(0.1)
        if (await service.get(rid)) is not None:
            return
    raise AssertionError(f"pending row {rid} never landed in DB")


@pytest.mark.asyncio
async def test_decide_resolves_local_futures_when_notify_raises(
    approval_service: ApprovalService,
) -> None:
    """Monkeypatch ``_notify.notify`` to raise. Call decide(). Local
    dispatcher future must still resolve with the correct decision and
    the row must be APPROVED.

    Proves F3: post-transition operations are best-effort and must not
    strand local waiters even on error.
    """
    tag = _tag()
    rid = f"req_f3_notify_{tag}"
    waiter = asyncio.create_task(
        approval_service.request_and_wait(
            request_id=rid,
            zone_id=f"z_{tag}",
            kind=ApprovalKind.EGRESS_HOST,
            subject=f"f3.notify:443:{tag}",
            agent_id="ag",
            token_id="tok",
            session_id=f"tok:s:{tag}",
            reason="r",
            metadata={},
        )
    )
    await _wait_pending(approval_service, rid)

    # Patch the bridge so notify() raises. Save the original so we can
    # restore after — other tests in the same fixture reuse the bridge.
    bridge = approval_service._notify
    original_notify = bridge.notify

    async def _boom(channel: str, payload: str) -> None:
        raise RuntimeError("simulated notify failure")

    # Tests are excluded from mypy (see .pre-commit-config.yaml) so a
    # plain attribute assignment is fine — monkeypatch the bound method.
    bridge.notify = _boom
    try:
        # decide() must NOT raise even though notify() blows up.
        decided = await approval_service.decide(
            request_id=rid,
            decision=Decision.APPROVED,
            decided_by="op",
            scope=DecisionScope.ONCE,
            reason=None,
            source=DecisionSource.GRPC,
        )
        assert decided.status is ApprovalRequestStatus.APPROVED

        # The waiting caller still got their decision.
        assert await asyncio.wait_for(waiter, 5.0) is Decision.APPROVED

        # And the row is durably APPROVED.
        row = await approval_service.get(rid)
        assert row is not None
        assert row.status is ApprovalRequestStatus.APPROVED
    finally:
        bridge.notify = original_notify


@pytest.mark.asyncio
async def test_decide_resolves_local_futures_when_session_allow_raises(
    approval_service: ApprovalService,
) -> None:
    """Monkeypatch ``repo.insert_session_allow`` to raise. Call decide()
    with SESSION scope. Local future must resolve and the row must be
    APPROVED — a session_allow miss is acceptable (reconciliation can
    address later) but stranding a waiter is not.
    """
    tag = _tag()
    rid = f"req_f3_session_{tag}"
    waiter = asyncio.create_task(
        approval_service.request_and_wait(
            request_id=rid,
            zone_id=f"z_{tag}",
            kind=ApprovalKind.EGRESS_HOST,
            subject=f"f3.session:443:{tag}",
            agent_id="ag",
            token_id="tok",
            session_id=f"tok:s:{tag}",
            reason="r",
            metadata={},
        )
    )
    await _wait_pending(approval_service, rid)

    repo = approval_service.repository
    original_insert = repo.insert_session_allow

    async def _boom(**kwargs):
        raise RuntimeError("simulated session_allow failure")

    repo.insert_session_allow = _boom
    try:
        decided = await approval_service.decide(
            request_id=rid,
            decision=Decision.APPROVED,
            decided_by="op",
            scope=DecisionScope.SESSION,
            reason=None,
            source=DecisionSource.GRPC,
        )
        assert decided.status is ApprovalRequestStatus.APPROVED
        assert await asyncio.wait_for(waiter, 5.0) is Decision.APPROVED

        row = await approval_service.get(rid)
        assert row is not None
        assert row.status is ApprovalRequestStatus.APPROVED
    finally:
        repo.insert_session_allow = original_insert
