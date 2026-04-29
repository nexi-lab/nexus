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
    """Test auth shim — always returns a static token id."""

    async def authorize(self, context: Any, capability: str) -> str:
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
    server = grpc.aio.server()
    approvals_pb2_grpc.add_ApprovalsV1Servicer_to_server(
        ApprovalsServicer(approval_service, auth=_AllowAllAuth()), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = approvals_pb2_grpc.ApprovalsV1Stub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await stub.Decide(
                    approvals_pb2.DecideRequest(
                        request_id="any",
                        decision="WAT",
                        scope="once",
                    )
                )
            assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    finally:
        await server.stop(grace=0.1)
