"""Integration tests for the Python gRPC server lifespan helpers.

Boots the gRPC server in-process (NOT via ``nexus up``) and exercises:
  - the bearer-token auth path (UNAUTHENTICATED on bad creds, OK on good)
  - lifespan start/stop semantics (graceful shutdown drains the port)

We use ``approval_service`` from the brick conftest so the test runs
against a real PostgreSQL backend. The gRPC server only needs the
service handle and the auth shim — no full FastAPI app required.
"""

from __future__ import annotations

import socket
import uuid
from contextlib import closing

import grpc
import grpc.aio
import pytest

from nexus.bricks.approvals.grpc_auth import BearerTokenCapabilityAuth
from nexus.bricks.approvals.grpc_server_lifespan import (
    start_grpc_server,
    stop_grpc_server,
)
from nexus.bricks.approvals.service import ApprovalService
from nexus.grpc.approvals import approvals_pb2, approvals_pb2_grpc

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _tag() -> str:
    return uuid.uuid4().hex[:12]


@pytest.mark.asyncio
async def test_bearer_auth_rejects_missing_token(approval_service: ApprovalService) -> None:
    """ListPending with no Authorization header must return UNAUTHENTICATED."""
    admin_token = f"tok_{_tag()}"
    auth = BearerTokenCapabilityAuth(admin_token=admin_token)
    port = _free_port()
    server = await start_grpc_server(approval_service, auth, port=port)

    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = approvals_pb2_grpc.ApprovalsV1Stub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await stub.ListPending(approvals_pb2.ListPendingRequest(zone_id=""))
            assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        await stop_grpc_server(server, grace_seconds=0.1)


@pytest.mark.asyncio
async def test_bearer_auth_rejects_wrong_token(approval_service: ApprovalService) -> None:
    """ListPending with a wrong Bearer token must return UNAUTHENTICATED."""
    admin_token = f"tok_{_tag()}"
    auth = BearerTokenCapabilityAuth(admin_token=admin_token)
    port = _free_port()
    server = await start_grpc_server(approval_service, auth, port=port)

    try:
        metadata = (("authorization", "Bearer not-the-right-token"),)
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = approvals_pb2_grpc.ApprovalsV1Stub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await stub.ListPending(
                    approvals_pb2.ListPendingRequest(zone_id=""),
                    metadata=metadata,
                )
            assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        await stop_grpc_server(server, grace_seconds=0.1)


@pytest.mark.asyncio
async def test_bearer_auth_accepts_correct_token(approval_service: ApprovalService) -> None:
    """ListPending with the correct Bearer token must succeed."""
    admin_token = f"tok_{_tag()}"
    auth = BearerTokenCapabilityAuth(admin_token=admin_token)
    port = _free_port()
    server = await start_grpc_server(approval_service, auth, port=port)

    try:
        metadata = (("authorization", f"Bearer {admin_token}"),)
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = approvals_pb2_grpc.ApprovalsV1Stub(channel)
            resp = await stub.ListPending(
                approvals_pb2.ListPendingRequest(zone_id=f"zone_{_tag()}"),
                metadata=metadata,
            )
            # No pending rows in this zone → empty list, no exception.
            assert list(resp.requests) == []
    finally:
        await stop_grpc_server(server, grace_seconds=0.1)


@pytest.mark.asyncio
async def test_stop_grpc_server_releases_port(approval_service: ApprovalService) -> None:
    """After stop_grpc_server, the bound port must be reusable."""
    admin_token = f"tok_{_tag()}"
    auth = BearerTokenCapabilityAuth(admin_token=admin_token)
    port = _free_port()

    server1 = await start_grpc_server(approval_service, auth, port=port)
    await stop_grpc_server(server1, grace_seconds=0.1)

    # Re-binding the same port should succeed once the first server is gone.
    server2 = await start_grpc_server(approval_service, auth, port=port)
    try:
        # Quick smoke-test that the new server actually works.
        metadata = (("authorization", f"Bearer {admin_token}"),)
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = approvals_pb2_grpc.ApprovalsV1Stub(channel)
            resp = await stub.ListPending(
                approvals_pb2.ListPendingRequest(zone_id=f"zone_{_tag()}"),
                metadata=metadata,
            )
            assert list(resp.requests) == []
    finally:
        await stop_grpc_server(server2, grace_seconds=0.1)
