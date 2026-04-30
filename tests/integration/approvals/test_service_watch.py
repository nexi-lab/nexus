"""ApprovalService.watch tests."""

import asyncio
import uuid

import pytest

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


@pytest.mark.asyncio
async def test_watch_emits_pending_then_decided(approval_service: ApprovalService):
    tag = _tag()
    zone = f"z_{tag}"
    rid = f"req_w_{tag}"
    events: list[tuple[str, str, str | None]] = []
    stop = asyncio.Event()

    async def consume():
        async for ev in approval_service.watch(zone_id=zone):
            events.append((ev.type, ev.request_id, ev.decision))
            if ev.type == "decided":
                stop.set()
                return

    task = asyncio.create_task(consume())
    # Give the watcher time to register before any events fire.
    await asyncio.sleep(0.05)

    waiter = asyncio.create_task(
        approval_service.request_and_wait(
            request_id=rid,
            zone_id=zone,
            kind=ApprovalKind.EGRESS_HOST,
            subject=f"watch.example:443:{tag}",
            agent_id="ag",
            token_id="tok",
            session_id=f"tok:s:{tag}",
            reason="r",
            metadata={},
        )
    )
    # Wait until the pending row is durably committed before deciding.
    # Sleeping a fixed delay is racy under xdist contention.
    for _ in range(50):
        await asyncio.sleep(0.1)
        if (await approval_service.get(rid)) is not None:
            break
    else:
        raise AssertionError("pending row never landed in DB")
    await approval_service.decide(
        request_id=rid,
        decision=Decision.APPROVED,
        decided_by="op",
        scope=DecisionScope.ONCE,
        reason=None,
        source=DecisionSource.GRPC,
    )
    # 5s timeout: under xdist parallel load the LISTEN/NOTIFY round-trip
    # for watch broadcasts can take >1s.
    await asyncio.wait_for(waiter, 5.0)
    await asyncio.wait_for(stop.wait(), 5.0)
    task.cancel()

    types = [e[0] for e in events]
    assert "pending" in types and "decided" in types
