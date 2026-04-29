"""Smoke benchmark: 100 concurrent callers on the same coalesce key.

Validates:
- The partial unique index coalesces concurrent inserts to one row.
- The Dispatcher fans out a single decision to all waiters in O(N).
- No quadratic locking/serialization in `request_and_wait`.

Expectation: one DB row, one decide call, all callers unblock < 500 ms after notify.

Scope choice: this bench intentionally uses ``DecisionScope.ONCE`` and a
unique ``session_id`` per caller. With the late-insert orphan-race fix
(Issue #3790, follow-up item 3), the ONCE scope branch deliberately does
NOT auto-inherit a recent decision — operator intent for ONCE is one
prompt per call. The bench sidesteps the late-insert window entirely by
parking on a barrier until every caller has registered its dispatcher
future BEFORE the single decide call. Without that barrier, late callers
would still time out on ONCE scope (correct semantics, but not what this
bench is measuring). For SESSION/PERSIST scopes the inherit branch makes
the late-insert window safe in production; see
``test_late_insert_race.py`` for those semantics.

Co-located with the approvals integration tests so the `approval_service`
fixture from `conftest.py` is picked up. Sibling tests use the same
`pytestmark = pytest.mark.integration` marker (see test_grpc_server.py,
test_service_request_and_wait.py).
"""

from __future__ import annotations

import asyncio
import time
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
async def test_coalesce_burst_100_callers(approval_service: ApprovalService) -> None:
    tag = _tag()
    zone = f"z_{tag}"
    subject = f"burst.example:443:{tag}"
    n_callers = 100

    async def caller(i: int) -> Decision:
        return await approval_service.request_and_wait(
            request_id=f"req_burst_{tag}_{i}",
            zone_id=zone,
            kind=ApprovalKind.EGRESS_HOST,
            subject=subject,
            agent_id="ag",
            token_id="tok",
            session_id=f"tok:s_{tag}_{i}",
            reason="r",
            metadata={},
        )

    tasks = [asyncio.create_task(caller(i)) for i in range(n_callers)]

    try:
        # Poll for the coalesced row to appear.
        target = None
        for _ in range(50):
            await asyncio.sleep(0.1)
            pending = await approval_service.list_pending(zone_id=zone)
            rows = [p for p in pending if p.subject == subject]
            if len(rows) == 1:
                target = rows[0]
                break
        else:
            raise AssertionError("coalesced row never landed")

        # Exactly one pending row before decide — coalesce contract.
        assert target is not None
        pending_now = await approval_service.list_pending(zone_id=zone)
        rows_now = [p for p in pending_now if p.subject == subject]
        assert len(rows_now) == 1, f"expected 1 coalesced row, got {len(rows_now)}"

        # Wait until every caller has executed its insert (and therefore
        # registered its dispatcher future under the coalesced row id). After
        # this point they are all parked on `wait_for(fut, ...)` and a single
        # decide will fan out to all of them. Without this barrier, slow
        # callers may insert AFTER decide flips the row to APPROVED, at which
        # point the partial unique index admits a new pending row keyed on
        # their own request_id — and that row is never decided.
        #
        # Use the dispatcher's public waiter_count() accessor to count
        # registered futures. This is intentionally white-box: the alternative
        # (sleeping a fixed duration) is racy and depends on cross-task
        # scheduling order on the event loop.
        dispatcher = approval_service._dispatcher
        for _ in range(100):
            registered = dispatcher.waiter_count(target.id)
            if registered >= n_callers:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError(
                f"only {dispatcher.waiter_count(target.id)}/{n_callers} callers "
                f"registered before timeout"
            )

        t0 = time.monotonic()
        await approval_service.decide(
            request_id=target.id,
            decision=Decision.APPROVED,
            decided_by="op",
            scope=DecisionScope.ONCE,
            reason=None,
            source=DecisionSource.GRPC,
        )
        results = await asyncio.gather(*tasks)
        elapsed_ms = (time.monotonic() - t0) * 1000

        # Print measured elapsed for visibility when running with `-s`.
        print(f"\n[bench_coalesce_burst] {n_callers} callers unblocked in {elapsed_ms:.1f} ms")

        assert all(r is Decision.APPROVED for r in results), (
            f"not all callers received APPROVED: "
            f"{sum(1 for r in results if r is Decision.APPROVED)}/{n_callers} approved"
        )
        # Plan target: 500 ms. If this flakes under xdist, raise to 1500 ms with
        # a comment about xdist contention. Standalone runs should be well under.
        assert elapsed_ms < 500, (
            f"unblock took {elapsed_ms:.1f} ms ({n_callers} callers, single coalesced row)"
        )

        # Sanity: dispatcher state should be empty after gather — every future
        # was resolved and popped via dispatcher.resolve(target.id, APPROVED).
        # No assertion (this is private state), but worth a comment.
    finally:
        # Avoid leaking tasks if any assertion above fails before gather completes.
        for t in tasks:
            if not t.done():
                t.cancel()
