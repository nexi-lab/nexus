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

    Implements both ``authorize`` and ``check_capability`` (Issue #3790
    F1): the servicer uses ``authorize`` for ListPending/Watch/Submit
    and ``check_capability`` for Get/Decide/Cancel.
    """

    async def authorize(self, context: Any, capability: str, zone_id: str) -> str:
        return "tok_test"

    async def check_capability(self, context: Any, capability: str, zone_id: str) -> str | None:
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
