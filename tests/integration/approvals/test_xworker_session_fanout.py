"""Cross-worker SESSION fan-out integration test (Issue #3790, F2).

Validates that every worker that has a local waiter for a request_id
writes its OWN ``session_allow`` rows on receiving the decided NOTIFY,
so the losing waiter's session is cached even though the deciding
worker never saw their session_id. Without F2, only the deciding
worker's local sessions get persisted; cross-worker waiters fall back
to a fresh PENDING row on the next same-session call.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from nexus.bricks.approvals.config import ApprovalConfig
from nexus.bricks.approvals.events import NotifyBridge
from nexus.bricks.approvals.models import (
    ApprovalKind,
    Decision,
    DecisionScope,
    DecisionSource,
)
from nexus.bricks.approvals.repository import ApprovalRepository
from nexus.bricks.approvals.service import ApprovalService

pytestmark = pytest.mark.integration


def _tag() -> str:
    return uuid.uuid4().hex[:12]


@pytest.mark.asyncio
async def test_session_allow_fans_out_to_other_workers_via_notify(
    session_factory, asyncpg_pool
) -> None:
    """Two workers, same DB. Caller A on service-1 and caller B on
    service-2 coalesce on (zone, kind, subject) with DIFFERENT
    session_ids. Operator decides on service-1 with SESSION scope.

    Both session_allow rows must exist after a brief settle delay —
    proves service-2's NOTIFY consumer wrote B's session_allow even
    though service-1 (the deciding worker) never saw B's session.
    """
    repo = ApprovalRepository(session_factory)
    cfg = ApprovalConfig(enabled=True)

    bridge_1 = NotifyBridge(asyncpg_pool)
    bridge_2 = NotifyBridge(asyncpg_pool)
    svc_1 = ApprovalService(repo, bridge_1, cfg)
    svc_2 = ApprovalService(repo, bridge_2, cfg)
    await svc_1.start()
    await svc_2.start()

    try:
        tag = _tag()
        zone = f"z_xworker_{tag}"
        subject = f"xworker.example:443:{tag}"
        kind = ApprovalKind.EGRESS_HOST
        a_session = f"tok:sA_{tag}"
        b_session = f"tok:sB_{tag}"

        # Caller A on service-1.
        task_a = asyncio.create_task(
            svc_1.request_and_wait(
                request_id=f"req_xA_{tag}",
                zone_id=zone,
                kind=kind,
                subject=subject,
                agent_id="ag-A",
                token_id="tok-A",
                session_id=a_session,
                reason="A",
                metadata={},
            )
        )
        # Wait for A's pending row to land.
        coalesced_id: str | None = None
        for _ in range(50):
            await asyncio.sleep(0.1)
            pending = await svc_1.list_pending(zone_id=zone)
            rows = [p for p in pending if p.subject == subject]
            if len(rows) == 1:
                coalesced_id = rows[0].id
                break
        assert coalesced_id is not None, "A's pending row never landed"

        # Caller B on service-2 — coalesces onto the same row.
        task_b = asyncio.create_task(
            svc_2.request_and_wait(
                request_id=f"req_xB_{tag}",
                zone_id=zone,
                kind=kind,
                subject=subject,
                agent_id="ag-B",
                token_id="tok-B",
                session_id=b_session,
                reason="B",
                metadata={},
            )
        )
        # Wait until svc_2's dispatcher actually has B parked on the
        # coalesced id — otherwise the NOTIFY may arrive before B
        # registers.
        for _ in range(50):
            await asyncio.sleep(0.1)
            if svc_2._dispatcher.waiter_count(coalesced_id) >= 1:
                break
        else:
            raise AssertionError("B never registered against the coalesced row")

        # Operator decides on svc_1 with SESSION scope.
        await svc_1.decide(
            request_id=coalesced_id,
            decision=Decision.APPROVED,
            decided_by="op",
            scope=DecisionScope.SESSION,
            reason=None,
            source=DecisionSource.GRPC,
        )

        # Both waiters resolve.
        assert await asyncio.wait_for(task_a, 5.0) is Decision.APPROVED
        assert await asyncio.wait_for(task_b, 5.0) is Decision.APPROVED

        # Allow svc_2's _on_decided_payload coroutine to drain.
        for _ in range(50):
            await asyncio.sleep(0.1)
            if await repo.session_allow_exists(
                session_id=b_session, zone_id=zone, kind=kind, subject=subject
            ):
                break

        # Both A and B's sessions must have allow rows.
        assert await repo.session_allow_exists(
            session_id=a_session, zone_id=zone, kind=kind, subject=subject
        ), "A's session_allow missing (deciding worker)"
        assert await repo.session_allow_exists(
            session_id=b_session, zone_id=zone, kind=kind, subject=subject
        ), "B's session_allow missing (cross-worker NOTIFY consumer)"
    finally:
        await svc_2.stop()
        await svc_1.stop()
