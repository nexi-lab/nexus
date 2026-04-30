"""Periodic reconcile watchdog integration test (Issue #3790, F3).

Validates that ApprovalService's watchdog task runs ``reconcile_in_flight``
on a fixed cadence so cross-worker waiters converge even when NOTIFY
delivery silently fails (asyncpg listener disconnect, missed payload, ...).
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
async def test_watchdog_resolves_future_without_notify(session_factory, asyncpg_pool) -> None:
    """Caller A waits on svc-1; svc-2 decides; svc-1's NOTIFY listener is
    detached so no NOTIFY callback fires locally. The watchdog must still
    resolve A's future via periodic reconcile.

    Reproduces the failure mode F3 calls out: NotifyBridge holds one
    asyncpg connection with no auto-reconnect. If that connection
    silently drops, decided rows never reach local waiters until they
    time out — unless the watchdog is running.
    """
    repo = ApprovalRepository(session_factory)
    # Tight reconcile interval so the test doesn't have to wait 30s.
    cfg_short = ApprovalConfig(enabled=True, reconcile_interval_seconds=0.5)

    bridge_1 = NotifyBridge(asyncpg_pool)
    bridge_2 = NotifyBridge(asyncpg_pool)
    svc_1 = ApprovalService(repo, bridge_1, cfg_short)
    svc_2 = ApprovalService(repo, bridge_2, ApprovalConfig(enabled=True))
    await svc_1.start()
    await svc_2.start()

    try:
        tag = _tag()
        zone = f"z_recon_{tag}"
        subject = f"recon-watchdog.example:443:{tag}"
        rid = f"req_w_{tag}"

        # Caller A waits on svc-1.
        waiter = asyncio.create_task(
            svc_1.request_and_wait(
                request_id=rid,
                zone_id=zone,
                kind=ApprovalKind.EGRESS_HOST,
                subject=subject,
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
            if (await svc_1.get(rid)) is not None:
                break
        else:
            raise AssertionError("pending row never landed in DB")

        # Detach svc-1's NOTIFY listeners. Any decided NOTIFY published
        # after this point WON'T reach svc-1's _on_decided_payload — only
        # the watchdog can resolve the future now.
        if bridge_1._listen_conn is not None:
            for channel in list(bridge_1._handlers):
                try:
                    await bridge_1._listen_conn.remove_listener(channel, bridge_1._on_notify)
                except Exception:
                    pass
            bridge_1._handlers = {}

        # Decide via svc-2 — this commits the row and emits NOTIFY, but
        # svc-1's listener is detached so it never receives the payload.
        await svc_2.decide(
            request_id=rid,
            decision=Decision.APPROVED,
            decided_by="op",
            scope=DecisionScope.ONCE,
            reason=None,
            source=DecisionSource.GRPC,
        )

        # The watchdog should pick this up within ~1s (interval=0.5s).
        # Allow up to 5s as slack for CI scheduling jitter.
        result = await asyncio.wait_for(waiter, 5.0)
        assert result is Decision.APPROVED
    finally:
        await svc_1.stop()
        await svc_2.stop()


@pytest.mark.asyncio
async def test_watchdog_disabled_when_interval_zero(session_factory, asyncpg_pool) -> None:
    """``reconcile_interval_seconds=0`` disables the watchdog (no task).

    Lets tests opt out of the periodic loop without monkeypatching.
    """
    repo = ApprovalRepository(session_factory)
    cfg = ApprovalConfig(enabled=True, reconcile_interval_seconds=0.0)
    bridge = NotifyBridge(asyncpg_pool)
    svc = ApprovalService(repo, bridge, cfg)
    await svc.start()
    try:
        assert svc._reconcile_task is None
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_watchdog_task_cancelled_on_stop(session_factory, asyncpg_pool) -> None:
    """``stop()`` cancels the watchdog task cleanly (no leaked coroutine)."""
    repo = ApprovalRepository(session_factory)
    cfg = ApprovalConfig(enabled=True, reconcile_interval_seconds=0.5)
    bridge = NotifyBridge(asyncpg_pool)
    svc = ApprovalService(repo, bridge, cfg)
    await svc.start()
    task = svc._reconcile_task
    assert task is not None
    assert not task.done()
    await svc.stop()
    assert svc._reconcile_task is None
    assert task.done()
