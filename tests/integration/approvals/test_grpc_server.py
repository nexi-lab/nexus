"""Integration tests for the ApprovalsV1 gRPC servicer."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import grpc
import grpc.aio
import pytest

from nexus.bricks.approvals.grpc_server import ApprovalsServicer
from nexus.bricks.approvals.models import (
    ApprovalKind,
    Decision,
    DecisionScope,
    DecisionSource,
)
from nexus.bricks.approvals.service import ApprovalService
from nexus.grpc.approvals import approvals_pb2, approvals_pb2_grpc

pytestmark = pytest.mark.integration


def _tag() -> str:
    return uuid.uuid4().hex[:12]


async def _wait_pending(service: ApprovalService, rid: str) -> None:
    """Poll until the pending row is durably committed.

    Fixed sleeps race under xdist parallel load — the request_and_wait
    insert may not have landed yet when the test moves on.
    """
    for _ in range(50):
        await asyncio.sleep(0.1)
        if (await service.get(rid)) is not None:
            return
    raise AssertionError(f"pending row {rid} never landed in DB")


class _AllowAllAuth:
    """Test auth shim — always returns a static token id.

    Implements ``authorize``, ``check_capability`` (Issue #3790 F1)
    and ``authenticate_only`` (#3790 F2 — pre-auth on Get/Decide/Cancel).
    """

    async def authorize(self, context: Any, capability: str, zone_id: str) -> str:
        return "tok_test"

    async def check_capability(self, context: Any, capability: str, zone_id: str) -> str | None:
        return "tok_test"

    async def authenticate_only(self, context: Any) -> str:
        return "tok_test"


@pytest.mark.asyncio
async def test_list_pending_returns_pending_rows(approval_service: ApprovalService) -> None:
    tag = _tag()
    rid = f"req_g1_{tag}"
    zone = f"z_{tag}"
    subject = f"grpc.example:443:{tag}"

    server = grpc.aio.server()
    approvals_pb2_grpc.add_ApprovalsV1Servicer_to_server(
        ApprovalsServicer(approval_service, auth=_AllowAllAuth()), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        waiter = asyncio.create_task(
            approval_service.request_and_wait(
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
        await _wait_pending(approval_service, rid)

        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = approvals_pb2_grpc.ApprovalsV1Stub(channel)
            resp = await stub.ListPending(approvals_pb2.ListPendingRequest(zone_id=zone))
            assert any(r.subject == subject for r in resp.requests)

        await approval_service.decide(
            request_id=rid,
            decision=Decision.APPROVED,
            decided_by="op",
            scope=DecisionScope.ONCE,
            reason=None,
            source=DecisionSource.GRPC,
        )
        assert (await asyncio.wait_for(waiter, 5.0)) is Decision.APPROVED
    finally:
        await server.stop(grace=0.1)


@pytest.mark.asyncio
async def test_decide_via_grpc_unblocks_waiter(approval_service: ApprovalService) -> None:
    tag = _tag()
    rid = f"req_g2_{tag}"
    zone = f"z_{tag}"
    subject = f"grpc2.example:443:{tag}"

    server = grpc.aio.server()
    approvals_pb2_grpc.add_ApprovalsV1Servicer_to_server(
        ApprovalsServicer(approval_service, auth=_AllowAllAuth()), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        waiter = asyncio.create_task(
            approval_service.request_and_wait(
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
        await _wait_pending(approval_service, rid)

        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = approvals_pb2_grpc.ApprovalsV1Stub(channel)
            await stub.Decide(
                approvals_pb2.DecideRequest(
                    request_id=rid,
                    decision="approved",
                    scope="once",
                    reason="ok",
                )
            )
        assert (await asyncio.wait_for(waiter, 5.0)) is Decision.APPROVED
    finally:
        await server.stop(grace=0.1)


@pytest.mark.asyncio
async def test_unknown_decision_value_returns_invalid_argument(
    approval_service: ApprovalService,
) -> None:
    """Decide with a malformed decision string returns INVALID_ARGUMENT.

    Per #3790 F1, Decide fetches the row first to scope the capability
    check to the row's zone, so the malformed-decision path requires a
    real pending row — otherwise we'd see NOT_FOUND first.
    """
    tag = _tag()
    rid = f"req_bad_decision_{tag}"
    zone = f"z_{tag}"
    subject = f"bad-decision.example:443:{tag}"

    server = grpc.aio.server()
    approvals_pb2_grpc.add_ApprovalsV1Servicer_to_server(
        ApprovalsServicer(approval_service, auth=_AllowAllAuth()), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        waiter = asyncio.create_task(
            approval_service.request_and_wait(
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
        await _wait_pending(approval_service, rid)
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = approvals_pb2_grpc.ApprovalsV1Stub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await stub.Decide(
                    approvals_pb2.DecideRequest(
                        request_id=rid,
                        decision="WAT",
                        scope="once",
                    )
                )
            assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        # Resolve the waiter via a clean Decide so the test doesn't leak
        # a pending coroutine awaiting timeout.
        await approval_service.decide(
            request_id=rid,
            decision=Decision.APPROVED,
            decided_by="op",
            scope=DecisionScope.ONCE,
            reason=None,
            source=DecisionSource.GRPC,
        )
        await asyncio.wait_for(waiter, 5.0)
    finally:
        await server.stop(grace=0.1)


# ---------------------------------------------------------------------------
# F1 — per-zone capability isolation (Issue #3790)
# ---------------------------------------------------------------------------


class _ZoneScopedAuth:
    """Auth shim — only authorizes the configured zone.

    Mirrors what ``ReBACCapabilityAuth`` does in production: a caller
    granted a capability on a specific zone gets that zone's RPCs but
    is denied on other zones. Used by the F1 integration tests to drive
    the cross-zone NOT_FOUND/PERMISSION_DENIED behavior in the
    servicer.
    """

    def __init__(self, allowed_zone: str) -> None:
        self._allowed_zone = allowed_zone

    async def authorize(self, context: Any, capability: str, zone_id: str) -> str:
        if zone_id != self._allowed_zone:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "zone denied")
            raise  # unreachable.
        return "tok_zoned"

    async def check_capability(self, context: Any, capability: str, zone_id: str) -> str | None:
        if zone_id != self._allowed_zone:
            return None
        return "tok_zoned"

    async def authenticate_only(self, context: Any) -> str:
        return "tok_zoned"


@pytest.mark.asyncio
async def test_list_pending_z2_with_z1_grant_returns_permission_denied(
    approval_service: ApprovalService,
) -> None:
    """ListPending(zone=z2) with auth granted only for z1 -> PERMISSION_DENIED.

    Proves the gRPC servicer scopes capability checks per-zone (F1).
    """
    tag = _tag()
    z1 = f"z1-{tag}"
    z2 = f"z2-{tag}"

    server = grpc.aio.server()
    approvals_pb2_grpc.add_ApprovalsV1Servicer_to_server(
        ApprovalsServicer(approval_service, auth=_ZoneScopedAuth(allowed_zone=z1)),
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = approvals_pb2_grpc.ApprovalsV1Stub(channel)
            # z1: granted -> success.
            ok = await stub.ListPending(approvals_pb2.ListPendingRequest(zone_id=z1))
            assert list(ok.requests) == []
            # z2: denied at the auth layer -> PERMISSION_DENIED.
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await stub.ListPending(approvals_pb2.ListPendingRequest(zone_id=z2))
            assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED
    finally:
        await server.stop(grace=0.1)


@pytest.mark.asyncio
async def test_get_cross_zone_returns_not_found(
    approval_service: ApprovalService,
) -> None:
    """Get(request_id) where the row's zone is z2 and caller has only z1 grant
    must return NOT_FOUND (not PERMISSION_DENIED) so request_id existence
    does not leak across zones (F1).
    """
    tag = _tag()
    z1 = f"z1-{tag}"
    z2 = f"z2-{tag}"
    rid_z2 = f"req_zoned_{tag}"

    server = grpc.aio.server()
    approvals_pb2_grpc.add_ApprovalsV1Servicer_to_server(
        ApprovalsServicer(approval_service, auth=_ZoneScopedAuth(allowed_zone=z1)),
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        # Plant a real pending row in z2 (the caller will not be able
        # to see it because they only have a z1 grant).
        waiter = asyncio.create_task(
            approval_service.request_and_wait(
                request_id=rid_z2,
                zone_id=z2,
                kind=ApprovalKind.EGRESS_HOST,
                subject=f"zoned.example:443:{tag}",
                agent_id="ag",
                token_id="tok",
                session_id=f"tok:s:{tag}",
                reason="r",
                metadata={},
            )
        )
        await _wait_pending(approval_service, rid_z2)

        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = approvals_pb2_grpc.ApprovalsV1Stub(channel)

            # Get on the z2 row from a z1-only caller -> NOT_FOUND
            # (NOT PERMISSION_DENIED — F1 leakage avoidance).
            with pytest.raises(grpc.aio.AioRpcError) as exc_get:
                await stub.Get(approvals_pb2.GetRequest(request_id=rid_z2))
            assert exc_get.value.code() == grpc.StatusCode.NOT_FOUND

            # Decide on the z2 row from a z1-only caller -> NOT_FOUND.
            with pytest.raises(grpc.aio.AioRpcError) as exc_dec:
                await stub.Decide(
                    approvals_pb2.DecideRequest(
                        request_id=rid_z2,
                        decision="approved",
                        scope="once",
                        reason="ok",
                    )
                )
            assert exc_dec.value.code() == grpc.StatusCode.NOT_FOUND

        # Cleanup: clear the waiter via a same-process Decide so it
        # doesn't leak to timeout.
        await approval_service.decide(
            request_id=rid_z2,
            decision=Decision.APPROVED,
            decided_by="op",
            scope=DecisionScope.ONCE,
            reason=None,
            source=DecisionSource.GRPC,
        )
        await asyncio.wait_for(waiter, 5.0)
    finally:
        await server.stop(grace=0.1)


# ---------------------------------------------------------------------------
# F1 (#3790) — Submit session_id is bound to authenticated token_id.
# ---------------------------------------------------------------------------


class _PerCallerAuth:
    """Auth shim that returns the same token_id for every call from one
    instance, but different instances return different token_ids.

    Used by the F1 cache-poisoning regression test: caller A and caller B
    each get their own _PerCallerAuth and submit through separate gRPC
    servers. The Submit handler binds the session_id to the
    authenticated token_id, so a session_allow row written for tokenA
    cannot short-circuit tokenB's Submit even when both clients pass
    the same client-side ``session_id`` value.
    """

    def __init__(self, token_id: str) -> None:
        self._token_id = token_id

    async def authorize(self, context: Any, capability: str, zone_id: str) -> str:
        return self._token_id

    async def check_capability(self, context: Any, capability: str, zone_id: str) -> str | None:
        return self._token_id

    async def authenticate_only(self, context: Any) -> str:
        return self._token_id


@pytest.mark.asyncio
async def test_submit_session_id_bound_to_authenticated_token(
    approval_service: ApprovalService,
) -> None:
    """Caller A's session_id="X" approval (SESSION) does NOT short-circuit
    caller B's Submit with session_id="X" — F1 cache-poisoning regression.

    Without per-token binding, ``approval_session_allow`` is keyed only on
    ``(session_id, zone_id, kind, subject)`` so any caller with
    ``approvals:request`` who knows or guesses a previously approved
    session_id could short-circuit operator decisions. The Submit handler
    namespaces session_id with ``grpc:{token_id}:`` to prevent this.
    """
    tag = _tag()
    zone = f"z_{tag}"
    subject = f"shared.example:443:{tag}"
    shared_session = f"shared_session_{tag}"

    # Server A: tokenA's Submit + decide creates a session_allow keyed on
    # ``grpc:tokenA:shared_session_{tag}``.
    server_a = grpc.aio.server()
    approvals_pb2_grpc.add_ApprovalsV1Servicer_to_server(
        ApprovalsServicer(approval_service, auth=_PerCallerAuth("tokenA")),
        server_a,
    )
    port_a = server_a.add_insecure_port("127.0.0.1:0")
    await server_a.start()

    # Server B: tokenB submits with the same client-supplied session_id.
    server_b = grpc.aio.server()
    approvals_pb2_grpc.add_ApprovalsV1Servicer_to_server(
        ApprovalsServicer(approval_service, auth=_PerCallerAuth("tokenB")),
        server_b,
    )
    port_b = server_b.add_insecure_port("127.0.0.1:0")
    await server_b.start()

    try:
        # ---- Caller A: Submit + operator-approve (SESSION scope). ----
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port_a}") as channel_a:
            stub_a = approvals_pb2_grpc.ApprovalsV1Stub(channel_a)

            async def submit_a() -> Any:
                return await stub_a.Submit(
                    approvals_pb2.SubmitRequest(
                        kind=ApprovalKind.EGRESS_HOST.value,
                        subject=subject,
                        zone_id=zone,
                        session_id=shared_session,
                        reason="caller A",
                        metadata_json="{}",
                        timeout_override_seconds=10.0,
                    )
                )

            submit_a_task = asyncio.create_task(submit_a())

            # Wait for the pending row, decide it via the service.
            pending_a = None
            for _ in range(50):
                await asyncio.sleep(0.1)
                pending = await approval_service.list_pending(zone_id=zone)
                match = [p for p in pending if p.subject == subject]
                if match:
                    pending_a = match[0]
                    break
            assert pending_a is not None, "tokenA pending row never landed"

            # Sanity: the stored session_id was bound to tokenA, not the
            # raw client value (this is the F1 fix).
            assert pending_a.session_id == f"grpc:tokenA:{shared_session}"

            await approval_service.decide(
                request_id=pending_a.id,
                decision=Decision.APPROVED,
                decided_by="op",
                scope=DecisionScope.SESSION,
                reason=None,
                source=DecisionSource.GRPC,
            )
            decision_a = await asyncio.wait_for(submit_a_task, 5.0)
            assert decision_a.decision == "approved"

        # Wait past the late-insert inherit grace window (_INHERIT_GRACE_SECONDS
        # = 2.0s in service.py) so caller B's submit doesn't inherit A's
        # APPROVED row via the orphan-race path. The vulnerability we are
        # testing is the SESSION-scope ``session_allow`` cache poisoning,
        # which is durable beyond the inherit window.
        await asyncio.sleep(2.5)

        # ---- Caller B: Submit with the same client-supplied session_id. ----
        # Without the binding fix, tokenB would short-circuit on the
        # session_allow row written for tokenA. With the fix, B's
        # server-side session_id is ``grpc:tokenB:...`` which has no
        # session_allow — so a fresh pending row is created and B's
        # Submit blocks on operator action.
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port_b}") as channel_b:
            stub_b = approvals_pb2_grpc.ApprovalsV1Stub(channel_b)

            async def submit_b() -> Any:
                return await stub_b.Submit(
                    approvals_pb2.SubmitRequest(
                        kind=ApprovalKind.EGRESS_HOST.value,
                        subject=subject,
                        zone_id=zone,
                        session_id=shared_session,
                        reason="caller B",
                        metadata_json="{}",
                        timeout_override_seconds=10.0,
                    )
                )

            submit_b_task = asyncio.create_task(submit_b())

            pending_b = None
            for _ in range(50):
                await asyncio.sleep(0.1)
                pending = await approval_service.list_pending(zone_id=zone)
                # Prior pending was decided; only B's row should be PENDING now.
                match = [p for p in pending if p.subject == subject]
                if match:
                    pending_b = match[0]
                    break
            assert pending_b is not None, (
                "tokenB Submit was short-circuited by tokenA's session_allow — "
                "F1 cross-token cache poisoning is still possible"
            )
            # B's row carries the tokenB-prefixed session_id (different
            # from A's), proving the namespace isolation.
            assert pending_b.session_id == f"grpc:tokenB:{shared_session}"
            assert pending_b.id != pending_a.id

            await approval_service.decide(
                request_id=pending_b.id,
                decision=Decision.APPROVED,
                decided_by="op",
                scope=DecisionScope.ONCE,
                reason=None,
                source=DecisionSource.GRPC,
            )
            decision_b = await asyncio.wait_for(submit_b_task, 5.0)
            assert decision_b.decision == "approved"
    finally:
        await server_a.stop(grace=0.1)
        await server_b.stop(grace=0.1)


@pytest.mark.asyncio
async def test_submit_empty_session_id_falls_back_to_token_namespace(
    approval_service: ApprovalService,
) -> None:
    """Submit with empty session_id still uses the bound ``grpc:{token_id}``
    namespace so SESSION-scope cache continues to work per-token.
    """
    tag = _tag()
    zone = f"z_{tag}"
    subject = f"empty-session.example:443:{tag}"

    server = grpc.aio.server()
    approvals_pb2_grpc.add_ApprovalsV1Servicer_to_server(
        ApprovalsServicer(approval_service, auth=_PerCallerAuth("tokenC")),
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = approvals_pb2_grpc.ApprovalsV1Stub(channel)

            async def submit() -> Any:
                return await stub.Submit(
                    approvals_pb2.SubmitRequest(
                        kind=ApprovalKind.EGRESS_HOST.value,
                        subject=subject,
                        zone_id=zone,
                        session_id="",  # client supplied nothing.
                        reason="r",
                        metadata_json="{}",
                        timeout_override_seconds=10.0,
                    )
                )

            submit_task = asyncio.create_task(submit())
            pending = None
            for _ in range(50):
                await asyncio.sleep(0.1)
                rows = await approval_service.list_pending(zone_id=zone)
                match = [p for p in rows if p.subject == subject]
                if match:
                    pending = match[0]
                    break
            assert pending is not None
            assert pending.session_id == "grpc:tokenC"

            await approval_service.decide(
                request_id=pending.id,
                decision=Decision.APPROVED,
                decided_by="op",
                scope=DecisionScope.ONCE,
                reason=None,
                source=DecisionSource.GRPC,
            )
            await asyncio.wait_for(submit_task, 5.0)
    finally:
        await server.stop(grace=0.1)


# ---------------------------------------------------------------------------
# F2 (#3790) — Get/Decide/Cancel must authenticate BEFORE the row lookup so
# response codes don't leak request_id existence to unauthenticated callers.
# ---------------------------------------------------------------------------


class _AuthRequiredAuth:
    """Test auth shim — aborts UNAUTHENTICATED unless an ``Authorization``
    metadata header was set. Used to drive F2 pre-auth regression tests
    so we can verify Get/Decide/Cancel surface UNAUTHENTICATED for an
    unauthenticated caller, not NOT_FOUND/OK (an existence oracle).
    """

    async def authorize(self, context: Any, capability: str, zone_id: str) -> str:
        await self._require_auth(context)
        return "tok_test"

    async def check_capability(self, context: Any, capability: str, zone_id: str) -> str | None:
        await self._require_auth(context)
        return "tok_test"

    async def authenticate_only(self, context: Any) -> str:
        await self._require_auth(context)
        return "tok_test"

    @staticmethod
    async def _require_auth(context: Any) -> None:
        for key, _ in context.invocation_metadata() or ():
            if key.lower() == "authorization":
                return
        await context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing authorization metadata")
        raise  # unreachable


@pytest.mark.asyncio
async def test_get_unauthenticated_returns_unauthenticated_not_not_found(
    approval_service: ApprovalService,
) -> None:
    """F2 (#3790): Get without Authorization metadata aborts UNAUTHENTICATED
    BEFORE the row lookup so the response code can't be used as an
    existence oracle for valid request_ids.
    """
    server = grpc.aio.server()
    approvals_pb2_grpc.add_ApprovalsV1Servicer_to_server(
        ApprovalsServicer(approval_service, auth=_AuthRequiredAuth()), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = approvals_pb2_grpc.ApprovalsV1Stub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await stub.Get(approvals_pb2.GetRequest(request_id="req_does_not_exist_x"))
            assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        await server.stop(grace=0.1)


@pytest.mark.asyncio
async def test_decide_unauthenticated_returns_unauthenticated_not_not_found(
    approval_service: ApprovalService,
) -> None:
    """F2 (#3790): same shape as the Get test, but for Decide."""
    server = grpc.aio.server()
    approvals_pb2_grpc.add_ApprovalsV1Servicer_to_server(
        ApprovalsServicer(approval_service, auth=_AuthRequiredAuth()), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = approvals_pb2_grpc.ApprovalsV1Stub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await stub.Decide(
                    approvals_pb2.DecideRequest(
                        request_id="req_does_not_exist_y",
                        decision="approved",
                        scope="once",
                    )
                )
            assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        await server.stop(grace=0.1)


@pytest.mark.asyncio
async def test_cancel_unauthenticated_returns_unauthenticated_not_ok(
    approval_service: ApprovalService,
) -> None:
    """F2 (#3790): Cancel must require auth even for unknown request_ids.

    Without the pre-auth gate, an unauthenticated caller can fire
    Cancel against arbitrary ids and always get OK back — a free
    zero-friction probing path. The fix forces a valid bearer token
    even when the row lookup will return None.
    """
    server = grpc.aio.server()
    approvals_pb2_grpc.add_ApprovalsV1Servicer_to_server(
        ApprovalsServicer(approval_service, auth=_AuthRequiredAuth()), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = approvals_pb2_grpc.ApprovalsV1Stub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await stub.Cancel(approvals_pb2.CancelRequest(request_id="req_unknown_z"))
            assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        await server.stop(grace=0.1)
