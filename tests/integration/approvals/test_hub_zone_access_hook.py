"""Gate-contract integration test for the hub zone-access hook (Task 19, #3790).

This test exercises ``PolicyGate.check`` directly against a live
``approval_service``. It is the contract that the hub zone-access
denial path will call when a token requests a zone outside its scope:
a pending row should be created with the supplied ``subject``, ``kind``,
and ``metadata``, and once an operator decides ``Decision.APPROVED`` the
gate returns ``Decision.APPROVED``.

The full hub-zone-access E2E test runs in Task 22 — this file only locks
the gate-call contract.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest

from nexus.bricks.approvals.models import (
    ApprovalKind,
    Decision,
    DecisionScope,
    DecisionSource,
)
from nexus.bricks.approvals.policy_gate import PolicyGate

pytestmark = pytest.mark.integration


def _tag() -> str:
    return uuid.uuid4().hex[:12]


async def _wait_pending_by_subject(service, zone_id: str, subject: str):
    """Poll list_pending(zone) for a row whose subject matches.

    PolicyGate.check generates its own request_id internally, so callers
    that only know (zone, subject) must poll by-subject. Mirrors the
    pattern used in test_mcp_egress_hook.py.
    """
    for _ in range(50):
        await asyncio.sleep(0.1)
        pending = await service.list_pending(zone_id=zone_id)
        match = [p for p in pending if p.subject == subject]
        if match:
            return match[0]
    raise AssertionError(f"pending row for subject {subject!r} never landed in DB")


@pytest.mark.asyncio
async def test_zone_access_creates_pending_request(approval_service):
    """The hub hook creates a pending row when a token misses zone scope
    and unblocks on approve.

    This locks the contract that the hub zone-access denial site will rely on:
      - ``subject`` round-trips verbatim (zone_id form).
      - ``kind`` is preserved as ``ZONE_ACCESS``.
      - ``metadata`` round-trips verbatim (requested_zone).
      - An operator approve unblocks the gate with ``Decision.APPROVED``.
    """
    gate = PolicyGate(approval_service)
    tag = _tag()
    zone = f"legal_{tag}"
    subject = zone  # For ZONE_ACCESS, subject == zone_id (the requested zone)
    token_id = f"tok_alice_{tag}"
    session_id = f"tok_alice:s:{tag}"

    async def caller():
        return await gate.check(
            kind=ApprovalKind.ZONE_ACCESS,
            subject=subject,
            zone_id=zone,
            token_id=token_id,
            session_id=session_id,
            agent_id=None,
            reason="zone_access",
            metadata={"requested_zone": zone},
        )

    waiter = asyncio.create_task(caller())

    try:
        pending = await _wait_pending_by_subject(approval_service, zone, subject)

        # Contract assertions — what the hub hook will rely on.
        assert pending.kind is ApprovalKind.ZONE_ACCESS
        assert pending.subject == subject
        assert pending.metadata.get("requested_zone") == zone
        assert pending.zone_id == zone
        assert pending.token_id == token_id
        assert pending.session_id == session_id
        assert pending.agent_id is None
        assert pending.reason == "zone_access"

        await approval_service.decide(
            request_id=pending.id,
            decision=Decision.APPROVED,
            decided_by="admin",
            scope=DecisionScope.PERSIST_SANDBOX,
            reason=None,
            source=DecisionSource.GRPC,
        )
        assert (await asyncio.wait_for(waiter, 5.0)) is Decision.APPROVED
    finally:
        if not waiter.done():
            waiter.cancel()
            with contextlib.suppress(BaseException):
                await waiter


@pytest.mark.asyncio
async def test_hub_session_id_does_not_short_circuit_session_cache(approval_service):
    """F2 (#3790): the synthesized ``hub:`` session_id MUST NOT auto-allow.

    Repeating ``_zone_access_approved_via_gate`` for the same user+zone
    after a prior SESSION-scope approval must NOT short-circuit on the
    session_allow row — the synthesized identifier has no HTTP-session
    lifetime, so durable caching against it would turn SESSION-scope into
    a permanent persist.

    The test simulates the hub call directly: PolicyGate.check with the
    same session_id pattern used by ``zone_routes._zone_access_approved_via_gate``
    is called once and SESSION-approved; a second call with the same
    identifiers must land a fresh pending row (no auto-allow).
    """
    from nexus.bricks.approvals.policy_gate import PolicyGate

    gate = PolicyGate(approval_service)
    tag = _tag()
    zone = f"sess_{tag}"
    user_id = f"u_{tag}"
    subject_type = "user"
    # Mirrors the synthesized identifiers in
    # nexus.server.auth.zone_routes._zone_access_approved_via_gate.
    token_id = f"hub:{subject_type}:{user_id}"
    session_id = f"{token_id}:zone:{zone}"

    async def call_once():
        return await gate.check(
            kind=ApprovalKind.ZONE_ACCESS,
            subject=zone,
            zone_id=zone,
            token_id=token_id,
            session_id=session_id,
            agent_id=None,
            reason="zone_access",
            metadata={"requested_zone": zone, "user_id": user_id},
        )

    # First call: SESSION-approve and verify decision.
    waiter1 = asyncio.create_task(call_once())
    pending1 = await _wait_pending_by_subject(approval_service, zone, zone)
    await approval_service.decide(
        request_id=pending1.id,
        decision=Decision.APPROVED,
        decided_by="op",
        scope=DecisionScope.SESSION,
        reason=None,
        source=DecisionSource.GRPC,
    )
    assert (await asyncio.wait_for(waiter1, 5.0)) is Decision.APPROVED

    # Second call (same identifiers) MUST NOT short-circuit. Without the
    # F2 fix, gate.check would consult ``session_allow_exists`` against
    # the stable ``hub:...`` session_id and return APPROVED without
    # creating a new row. With the fix, a fresh pending row appears.
    waiter2 = asyncio.create_task(call_once())

    pending2 = None
    try:
        for _ in range(50):
            await asyncio.sleep(0.1)
            pending = await approval_service.list_pending(zone_id=zone)
            # We expect a NEW pending row (different from pending1.id).
            match = [p for p in pending if p.subject == zone and p.id != pending1.id]
            if match:
                pending2 = match[0]
                break
        assert pending2 is not None, (
            "second hub-zone-access call short-circuited via session_allow "
            "instead of opening a fresh pending row — F2 regression"
        )
        assert pending2.session_id == session_id

        # Cleanup so the waiter doesn't leak: deny is fine.
        await approval_service.decide(
            request_id=pending2.id,
            decision=Decision.DENIED,
            decided_by="op",
            scope=DecisionScope.ONCE,
            reason="cleanup",
            source=DecisionSource.GRPC,
        )
        assert (await asyncio.wait_for(waiter2, 5.0)) is Decision.DENIED
    finally:
        if not waiter2.done():
            waiter2.cancel()
            with contextlib.suppress(BaseException):
                await waiter2


@pytest.mark.asyncio
async def test_zone_access_denied_returns_decision_denied(approval_service):
    """An operator-deny on a zone-access request returns Decision.DENIED — the
    hub hook will then keep its existing 403 deny path.
    """
    gate = PolicyGate(approval_service)
    tag = _tag()
    zone = f"forbidden_{tag}"
    subject = zone
    token_id = f"tok_bob_{tag}"
    session_id = f"tok_bob:s:{tag}"

    async def caller():
        return await gate.check(
            kind=ApprovalKind.ZONE_ACCESS,
            subject=subject,
            zone_id=zone,
            token_id=token_id,
            session_id=session_id,
            agent_id=None,
            reason="zone_access",
            metadata={"requested_zone": zone},
        )

    waiter = asyncio.create_task(caller())

    try:
        pending = await _wait_pending_by_subject(approval_service, zone, subject)
        assert pending.kind is ApprovalKind.ZONE_ACCESS

        await approval_service.decide(
            request_id=pending.id,
            decision=Decision.DENIED,
            decided_by="op",
            scope=DecisionScope.ONCE,
            reason="not allowed",
            source=DecisionSource.GRPC,
        )
        assert (await asyncio.wait_for(waiter, 5.0)) is Decision.DENIED
    finally:
        if not waiter.done():
            waiter.cancel()
            with contextlib.suppress(BaseException):
                await waiter
