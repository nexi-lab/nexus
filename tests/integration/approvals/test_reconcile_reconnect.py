"""Reconcile-on-reconnect integration test (Issue #3790)."""

import asyncio
import uuid
from datetime import UTC, datetime

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


@pytest.mark.asyncio
async def test_reconcile_resolves_pending_futures_for_decided_rows(
    approval_service: ApprovalService,
):
    tag = _tag()
    rid = f"req_recon_{tag}"
    waiter = asyncio.create_task(
        approval_service.request_and_wait(
            request_id=rid,
            zone_id=f"z_{tag}",
            kind=ApprovalKind.EGRESS_HOST,
            subject=f"recon:443:{tag}",
            agent_id="ag",
            token_id="tok",
            session_id=f"tok:s:{tag}",
            reason="r",
            metadata={},
        )
    )
    # Wait until the pending row is durably committed.
    for _ in range(50):
        await asyncio.sleep(0.1)
        if (await approval_service.get(rid)) is not None:
            break
    else:
        raise AssertionError("pending row never landed in DB")

    # Bypass NOTIFY: write the decision via the repo directly. The dispatcher
    # never gets a NOTIFY callback for this transition.
    await approval_service.repository.transition(
        request_id=rid,
        new_status=ApprovalRequestStatus.APPROVED,
        decided_by="op",
        scope=DecisionScope.ONCE,
        reason=None,
        source=DecisionSource.GRPC,
        now=datetime.now(UTC),
    )

    # Force reconciliation — the missing-NOTIFY recovery path.
    await approval_service.reconcile_in_flight()
    assert (await asyncio.wait_for(waiter, 1.0)) is Decision.APPROVED
