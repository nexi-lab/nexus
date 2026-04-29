"""Sweeper integration test."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from nexus.bricks.approvals.models import ApprovalKind
from nexus.bricks.approvals.repository import ApprovalRepository
from nexus.bricks.approvals.sweeper import Sweeper

pytestmark = pytest.mark.integration


def _tag() -> str:
    return uuid.uuid4().hex[:12]


@pytest.mark.asyncio
async def test_sweeper_expires_past_due_rows(session_factory):
    repo = ApprovalRepository(session_factory)
    tag = _tag()
    rid = f"req_old_s_{tag}"
    past = datetime.now(UTC) - timedelta(seconds=5)
    await repo.insert_or_fetch_pending(
        request_id=rid,
        zone_id=f"z_{tag}",
        kind=ApprovalKind.EGRESS_HOST,
        subject=f"old:443:{tag}",
        agent_id=None,
        token_id="tok",
        session_id=None,
        reason="",
        metadata={},
        now=past,
        expires_at=past,
    )

    expired_ids: list[str] = []
    sweeper = Sweeper(repo, interval_seconds=0.1, on_expired=lambda ids: expired_ids.extend(ids))
    await sweeper.start()
    try:
        # Poll until our specific id is expired (xdist-safe; other tests may
        # also be expiring rows in parallel).
        for _ in range(50):
            await asyncio.sleep(0.1)
            row = await repo.get(rid)
            if row and row.status.value == "expired":
                break
        else:
            raise AssertionError(f"sweeper did not expire {rid} in 5s")
    finally:
        await sweeper.stop()

    row = await repo.get(rid)
    assert row is not None and row.status.value == "expired"
