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
async def test_watchdog_writes_session_allow_on_session_scope_approved(
    session_factory, asyncpg_pool
) -> None:
    """F3 (#3790): when the watchdog recovers a SESSION-scope APPROVED row
    after a NOTIFY drop, it must also write the ``session_allow`` rows
    for every coalesced waiter on this worker — same fan-out shape as
    ``_on_decided_payload``. Without this, the deciding worker's
    ``decide`` only writes session_allow for ``row.session_id`` plus
    its OWN local dispatcher waiters; a coalesced waiter on a
    DIFFERENT worker (with a different ``session_id``) would never
    have its session_allow row persisted, and a subsequent same-session
    call from that worker would reopen a fresh PENDING.

    Uses two distinct session_ids: ``sid_row`` is on the durable row
    (carried by the first submit), ``sid_local_only`` is in svc_1's
    dispatcher only. The test asserts the watchdog wrote
    ``session_allow`` for ``sid_local_only`` — which the deciding
    worker (svc_2) had no way to know about and would never have
    written without the watchdog fan-out.
    """
    repo = ApprovalRepository(session_factory)
    cfg_short = ApprovalConfig(enabled=True, reconcile_interval_seconds=0.5)

    bridge_1 = NotifyBridge(asyncpg_pool)
    bridge_2 = NotifyBridge(asyncpg_pool)
    svc_1 = ApprovalService(repo, bridge_1, cfg_short)
    svc_2 = ApprovalService(repo, bridge_2, ApprovalConfig(enabled=True))
    await svc_1.start()
    await svc_2.start()

    try:
        tag = _tag()
        zone = f"z_recon_session_{tag}"
        subject = f"recon-session.example:443:{tag}"
        rid_row = f"req_ws_{tag}"
        rid_coalesced = f"req_ws_b_{tag}"
        sid_row = f"tok:s_row:{tag}"
        sid_local_only = f"tok:s_local:{tag}"

        # Submit 1: lands the durable row carrying sid_row.
        waiter_a = asyncio.create_task(
            svc_1.request_and_wait(
                request_id=rid_row,
                zone_id=zone,
                kind=ApprovalKind.EGRESS_HOST,
                subject=subject,
                agent_id="ag",
                token_id="tok",
                session_id=sid_row,
                reason="r",
                metadata={},
            )
        )

        # Wait until the pending row is durably committed.
        for _ in range(50):
            await asyncio.sleep(0.1)
            if (await svc_1.get(rid_row)) is not None:
                break
        else:
            raise AssertionError("pending row never landed in DB")

        # Submit 2 (same coalesce key, distinct session_id): coalesces
        # onto the same DB row but registers a SECOND future on svc_1's
        # dispatcher with sid_local_only. Only the local dispatcher
        # knows about sid_local_only — the deciding worker (svc_2)
        # has no way to discover it without NOTIFY.
        waiter_b = asyncio.create_task(
            svc_1.request_and_wait(
                request_id=rid_coalesced,
                zone_id=zone,
                kind=ApprovalKind.EGRESS_HOST,
                subject=subject,
                agent_id="ag",
                token_id="tok",
                session_id=sid_local_only,
                reason="r",
                metadata={},
            )
        )
        # Give the second submit a beat to register on the dispatcher.
        await asyncio.sleep(0.2)

        # Detach svc-1's NOTIFY listeners — decided NOTIFYs from svc-2
        # will not reach ``_on_decided_payload`` here, so only
        # ``reconcile_in_flight`` (the watchdog) can both resolve the
        # futures AND fan out session_allow.
        if bridge_1._listen_conn is not None:
            for channel in list(bridge_1._handlers):
                try:
                    await bridge_1._listen_conn.remove_listener(channel, bridge_1._on_notify)
                except Exception:
                    pass
            bridge_1._handlers = {}

        # Decide via svc-2 with SESSION scope.
        await svc_2.decide(
            request_id=rid_row,
            decision=Decision.APPROVED,
            decided_by="op",
            scope=DecisionScope.SESSION,
            reason=None,
            source=DecisionSource.GRPC,
        )

        # Watchdog must resolve BOTH futures AND write session_allow
        # for sid_local_only — the session_id only svc_1's dispatcher
        # knew about. Without the F3 fan-out, sid_local_only would
        # have no session_allow row, and the next same-session call
        # would reopen a fresh PENDING.
        result_a = await asyncio.wait_for(waiter_a, 5.0)
        result_b = await asyncio.wait_for(waiter_b, 5.0)
        assert result_a is Decision.APPROVED
        assert result_b is Decision.APPROVED
        # The watchdog resolves the futures BEFORE running the
        # session_allow inserts (best-effort fan-out, same shape as
        # ``_on_decided_payload``). Poll briefly so the test is not
        # racing the same-tick insert. If the F3 fan-out is missing,
        # the row never appears even after several watchdog ticks.
        for _ in range(50):
            if await repo.session_allow_exists(
                session_id=sid_local_only,
                zone_id=zone,
                kind=ApprovalKind.EGRESS_HOST,
                subject=subject,
            ):
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError(
                "F3: watchdog dropped session_allow fan-out for local-only session_id"
            )
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
