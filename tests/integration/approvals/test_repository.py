"""Repository integration tests (live Postgres).

Tests use uuid-prefixed identifiers so they remain isolated under parallel
execution (xdist) and across reruns without per-test cleanup.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from nexus.bricks.approvals.db_models import (
    ApprovalDecisionModel,
)
from nexus.bricks.approvals.models import (
    ApprovalKind,
    ApprovalRequestStatus,
    DecisionScope,
    DecisionSource,
)
from nexus.bricks.approvals.repository import ApprovalRepository

pytestmark = pytest.mark.integration


def _tag() -> str:
    """Per-test unique suffix for any value that participates in uniqueness."""
    return uuid.uuid4().hex[:12]


async def _new_repo(session_factory) -> ApprovalRepository:
    return ApprovalRepository(session_factory)


@pytest.mark.asyncio
async def test_insert_or_fetch_pending_coalesces_concurrent_inserts(session_factory):
    repo = await _new_repo(session_factory)
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=60)
    tag = _tag()
    zone = f"z_{tag}"
    subject = f"api.example.com:443:{tag}"

    a, b = await asyncio.gather(
        repo.insert_or_fetch_pending(
            request_id=f"req_a_{tag}",
            zone_id=zone,
            kind=ApprovalKind.EGRESS_HOST,
            subject=subject,
            agent_id="ag",
            token_id="tok",
            session_id=f"tok:s1:{tag}",
            reason="r",
            metadata={},
            now=now,
            expires_at=expires,
        ),
        repo.insert_or_fetch_pending(
            request_id=f"req_b_{tag}",
            zone_id=zone,
            kind=ApprovalKind.EGRESS_HOST,
            subject=subject,
            agent_id="ag",
            token_id="tok2",
            session_id=f"tok2:s1:{tag}",
            reason="r",
            metadata={},
            now=now,
            expires_at=expires,
        ),
    )
    assert a.id == b.id  # exactly one row


@pytest.mark.asyncio
async def test_decide_pending_to_approved_emits_audit_row(session_factory):
    repo = await _new_repo(session_factory)
    now = datetime.now(UTC)
    tag = _tag()
    rid = f"req_x_{tag}"
    req = await repo.insert_or_fetch_pending(
        request_id=rid,
        zone_id=f"z_{tag}",
        kind=ApprovalKind.ZONE_ACCESS,
        subject=f"legal_{tag}",
        agent_id=None,
        token_id="tok",
        session_id=None,
        reason="",
        metadata={},
        now=now,
        expires_at=now + timedelta(seconds=60),
    )
    updated = await repo.transition(
        request_id=req.id,
        new_status=ApprovalRequestStatus.APPROVED,
        decided_by="op",
        scope=DecisionScope.ONCE,
        reason="ok",
        source=DecisionSource.GRPC,
        now=now,
    )
    assert updated is not None and updated.status is ApprovalRequestStatus.APPROVED

    async with session_factory() as s:
        rows = (await s.execute(_select_decisions(req.id))).scalars().all()
        assert len(rows) == 1
        assert rows[0].decision == "approved"


@pytest.mark.asyncio
async def test_transition_returns_none_when_not_pending(session_factory):
    repo = await _new_repo(session_factory)
    now = datetime.now(UTC)
    tag = _tag()
    req = await repo.insert_or_fetch_pending(
        request_id=f"req_x2_{tag}",
        zone_id=f"z_{tag}",
        kind=ApprovalKind.ZONE_ACCESS,
        subject=f"legal_{tag}",
        agent_id=None,
        token_id="tok",
        session_id=None,
        reason="",
        metadata={},
        now=now,
        expires_at=now + timedelta(seconds=60),
    )
    await repo.transition(
        request_id=req.id,
        new_status=ApprovalRequestStatus.APPROVED,
        decided_by="op",
        scope=DecisionScope.ONCE,
        reason=None,
        source=DecisionSource.GRPC,
        now=now,
    )
    second = await repo.transition(
        request_id=req.id,
        new_status=ApprovalRequestStatus.REJECTED,
        decided_by="op2",
        scope=DecisionScope.ONCE,
        reason=None,
        source=DecisionSource.GRPC,
        now=now,
    )
    assert second is None


@pytest.mark.asyncio
async def test_session_allow_round_trip(session_factory):
    repo = await _new_repo(session_factory)
    now = datetime.now(UTC)
    tag = _tag()
    sid = f"tok:s1:{tag}"
    zone = f"z_{tag}"
    subject = f"api.example.com:443:{tag}"
    await repo.insert_session_allow(
        session_id=sid,
        zone_id=zone,
        kind=ApprovalKind.EGRESS_HOST,
        subject=subject,
        decided_by="op",
        decided_at=now,
        request_id=None,
    )
    assert await repo.session_allow_exists(
        session_id=sid,
        zone_id=zone,
        kind=ApprovalKind.EGRESS_HOST,
        subject=subject,
    )
    assert not await repo.session_allow_exists(
        session_id=sid,
        zone_id=zone,
        kind=ApprovalKind.EGRESS_HOST,
        subject=f"other:443:{tag}",
    )


@pytest.mark.asyncio
async def test_sweep_expired_marks_past_due_rows(session_factory):
    """Insert a past-due pending row, then verify sweep_expired drives it to
    'expired'. Under xdist, a sibling test's running sweeper may expire our row
    before our manual sweep — assert on the resulting row status, not on
    membership in this call's return list.
    """
    repo = await _new_repo(session_factory)
    now = datetime.now(UTC)
    past = now - timedelta(seconds=1)
    tag = _tag()
    rid = f"req_old_{tag}"
    await repo.insert_or_fetch_pending(
        request_id=rid,
        zone_id=f"z_{tag}",
        kind=ApprovalKind.EGRESS_HOST,
        subject=f"old.example:443:{tag}",
        agent_id=None,
        token_id="tok",
        session_id=None,
        reason="",
        metadata={},
        now=past,
        expires_at=past,
    )
    await repo.sweep_expired(now=now)
    row = await repo.get(rid)
    assert row is not None and row.status.value == "expired"


@pytest.mark.asyncio
async def test_transition_refuses_approved_when_expires_at_past(session_factory):
    """F1 (#3790): an operator decision arriving after expires_at must NOT
    flip a stale pending row to APPROVED/REJECTED.

    Without this guard, ``request_and_wait`` callers who time out locally
    leave a pending row in the DB until the periodic sweeper runs; an
    operator decision in that gap would write a SESSION-scope
    ``session_allow`` for an already-auto-denied request, granting
    future calls the original gate already rejected.

    EXPIRED transitions still succeed (the sweeper / local-timeout path
    drives those).
    """
    repo = await _new_repo(session_factory)
    now = datetime.now(UTC)
    past = now - timedelta(seconds=30)
    tag = _tag()
    rid = f"req_stale_{tag}"
    # Insert a pending row whose expires_at is already in the past.
    await repo.insert_or_fetch_pending(
        request_id=rid,
        zone_id=f"z_{tag}",
        kind=ApprovalKind.EGRESS_HOST,
        subject=f"stale.example:443:{tag}",
        agent_id=None,
        token_id="tok",
        session_id=None,
        reason="",
        metadata={},
        now=past,
        expires_at=past,
    )

    # APPROVED transition must be refused.
    refused = await repo.transition(
        request_id=rid,
        new_status=ApprovalRequestStatus.APPROVED,
        decided_by="op",
        scope=DecisionScope.SESSION,
        reason="op clicked approve",
        source=DecisionSource.GRPC,
        now=now,
    )
    assert refused is None
    row = await repo.get(rid)
    assert row is not None and row.status is ApprovalRequestStatus.PENDING

    # REJECTED transition is similarly refused.
    refused2 = await repo.transition(
        request_id=rid,
        new_status=ApprovalRequestStatus.REJECTED,
        decided_by="op",
        scope=DecisionScope.ONCE,
        reason=None,
        source=DecisionSource.GRPC,
        now=now,
    )
    assert refused2 is None
    row = await repo.get(rid)
    assert row is not None and row.status is ApprovalRequestStatus.PENDING

    # EXPIRED transition still succeeds (this is the sweeper path).
    # Under xdist a sibling test's running sweeper may have already
    # expired our row before we get here — assert on the resulting row
    # status, not on the transition() return. The point of the test is
    # that the new ``expires_at > now`` predicate is bypassed for the
    # EXPIRED branch; the row reaching EXPIRED state proves it.
    await repo.transition(
        request_id=rid,
        new_status=ApprovalRequestStatus.EXPIRED,
        decided_by="system",
        scope=DecisionScope.ONCE,
        reason="auto_deny_after_timeout",
        source=DecisionSource.SYSTEM_TIMEOUT,
        now=now,
    )
    row = await repo.get(rid)
    assert row is not None and row.status is ApprovalRequestStatus.EXPIRED


def _select_decisions(request_id: str):
    from sqlalchemy import select

    return select(ApprovalDecisionModel).where(ApprovalDecisionModel.request_id == request_id)
