"""Repository integration tests (live Postgres)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from nexus.bricks.approvals.db_models import (
    ApprovalDecisionModel,
)
from nexus.bricks.approvals.models import (
    ApprovalKind,
    DecisionScope,
    DecisionSource,
)
from nexus.bricks.approvals.repository import ApprovalRepository

pytestmark = pytest.mark.integration


async def _new_repo(session_factory) -> ApprovalRepository:
    return ApprovalRepository(session_factory)


@pytest.mark.asyncio
async def test_insert_or_fetch_pending_coalesces_concurrent_inserts(session_factory):
    repo = await _new_repo(session_factory)
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=60)

    a, b = await asyncio.gather(
        repo.insert_or_fetch_pending(
            request_id="req_a",
            zone_id="z",
            kind=ApprovalKind.EGRESS_HOST,
            subject="api.example.com:443",
            agent_id="ag",
            token_id="tok",
            session_id="tok:s1",
            reason="r",
            metadata={},
            now=now,
            expires_at=expires,
        ),
        repo.insert_or_fetch_pending(
            request_id="req_b",
            zone_id="z",
            kind=ApprovalKind.EGRESS_HOST,
            subject="api.example.com:443",
            agent_id="ag",
            token_id="tok2",
            session_id="tok2:s1",
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
    req = await repo.insert_or_fetch_pending(
        request_id="req_x",
        zone_id="z",
        kind=ApprovalKind.ZONE_ACCESS,
        subject="legal",
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
        new_status="approved",
        decided_by="op",
        scope=DecisionScope.ONCE,
        reason="ok",
        source=DecisionSource.GRPC,
        now=now,
    )
    assert updated is not None and updated.status == "approved"

    async with session_factory() as s:
        rows = (await s.execute(_select_decisions(req.id))).scalars().all()
        assert len(rows) == 1
        assert rows[0].decision == "approved"


@pytest.mark.asyncio
async def test_transition_returns_none_when_not_pending(session_factory):
    repo = await _new_repo(session_factory)
    now = datetime.now(UTC)
    req = await repo.insert_or_fetch_pending(
        request_id="req_x2",
        zone_id="z",
        kind=ApprovalKind.ZONE_ACCESS,
        subject="legal",
        agent_id=None,
        token_id="tok",
        session_id=None,
        reason="",
        metadata={},
        now=now,
        expires_at=now + timedelta(seconds=60),
    )
    await repo.transition(
        req.id, "approved", "op", DecisionScope.ONCE, None, DecisionSource.GRPC, now
    )
    second = await repo.transition(
        req.id, "rejected", "op2", DecisionScope.ONCE, None, DecisionSource.GRPC, now
    )
    assert second is None


@pytest.mark.asyncio
async def test_session_allow_round_trip(session_factory):
    repo = await _new_repo(session_factory)
    now = datetime.now(UTC)
    await repo.insert_session_allow(
        session_id="tok:s1",
        zone_id="z",
        kind=ApprovalKind.EGRESS_HOST,
        subject="api.example.com:443",
        decided_by="op",
        decided_at=now,
        request_id=None,
    )
    found = await repo.find_session_allow(
        session_id="tok:s1",
        zone_id="z",
        kind=ApprovalKind.EGRESS_HOST,
        subject="api.example.com:443",
    )
    assert found is not None
    miss = await repo.find_session_allow(
        session_id="tok:s1",
        zone_id="z",
        kind=ApprovalKind.EGRESS_HOST,
        subject="other:443",
    )
    assert miss is None


@pytest.mark.asyncio
async def test_sweep_expired_marks_and_returns_ids(session_factory):
    repo = await _new_repo(session_factory)
    now = datetime.now(UTC)
    past = now - timedelta(seconds=1)
    await repo.insert_or_fetch_pending(
        request_id="req_old",
        zone_id="z",
        kind=ApprovalKind.EGRESS_HOST,
        subject="old.example:443",
        agent_id=None,
        token_id="tok",
        session_id=None,
        reason="",
        metadata={},
        now=past,
        expires_at=past,
    )
    swept = await repo.sweep_expired(now=now)
    assert "req_old" in swept


def _select_decisions(request_id: str):
    from sqlalchemy import select

    return select(ApprovalDecisionModel).where(ApprovalDecisionModel.request_id == request_id)
