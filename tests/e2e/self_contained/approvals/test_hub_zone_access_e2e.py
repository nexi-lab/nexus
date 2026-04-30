"""E2E: hub zone-access approval — submit -> approve -> unblock.

Issue #3790, Task 22. Validates the gRPC contract for ZONE_ACCESS approvals
against a real ``nexus up --build`` stack.

Why a gRPC-contract test (not an HTTP path test)
================================================
The plan originally sketched calling ``GET /zones/<id>/health`` from a
``restricted_token`` client and asserting the request blocks until an
operator approves. Two reasons we stay with the gRPC pattern from Task 21:

  1. The hub zone-access gate (Task 19) is wired at
     ``GET /api/zones/{zone_id}`` in ``src/nexus/server/auth/zone_routes.py``,
     not ``/zones/<id>/health``. The plan's path doesn't match the
     real route.
  2. There is no ``restricted_token`` fixture exposed by
     ``tests/e2e/self_contained/conftest.py`` — only ``admin_token``.
     Building a scoped ReBAC token in this environment requires
     ReBAC-tuple plumbing that's out of scope for #3790.

The gRPC contract test is sufficient: ``Submit(kind=ZONE_ACCESS, ...)``
is the exact RPC the hub gate's PolicyGate hop makes when it pauses on a
zone-scope miss. The HTTP-path layer can be a follow-up once a real
``restricted_token`` fixture exists.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid

import grpc.aio
import pytest

from nexus.grpc.approvals import approvals_pb2, approvals_pb2_grpc

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(600)]


def _tag() -> str:
    """uuid-prefixed identifier — keeps tests xdist-safe."""
    return uuid.uuid4().hex[:12]


@pytest.mark.asyncio
async def test_zone_access_request_via_grpc(running_nexus):
    """Submit a ZONE_ACCESS request via gRPC, approve via second client.

    Two coroutines, each on its own gRPC channel:

      - "requester": calls ``Submit(kind=ZONE_ACCESS, subject=<zone_id>)``,
        which is the same RPC the hub zone-access gate (Task 19) issues
        when a token misses on a zone scope. Submit blocks until decide.

      - "operator": polls ``ListPending`` filtered to the same zone,
        finds the row by (kind, subject), calls ``Decide(approved)``.

    Once approved, the requester unblocks with
    ``SubmitDecision(decision="approved")`` — the exact value the hub
    gate compares against to release the original HTTP request.
    """
    tag = _tag()
    zone = f"legal_{tag}"
    # For ZONE_ACCESS, ``subject`` is the zone the caller wants to enter
    # — same convention used by the hub hook in Task 19.
    subject = zone
    auth_metadata: tuple = (("authorization", f"Bearer {running_nexus.admin_token}"),)

    async with (
        grpc.aio.insecure_channel(running_nexus.grpc_addr) as req_channel,
        grpc.aio.insecure_channel(running_nexus.grpc_addr) as op_channel,
    ):
        req_stub = approvals_pb2_grpc.ApprovalsV1Stub(req_channel)
        op_stub = approvals_pb2_grpc.ApprovalsV1Stub(op_channel)

        submit_req = approvals_pb2.SubmitRequest(
            kind="zone_access",
            subject=subject,
            zone_id=zone,
            token_id=f"tok_alice_{tag}",
            session_id=f"tok_alice_{tag}:s",
            agent_id="",
            reason="test zone access",
            metadata_json=json.dumps({"requested_zone": zone}),
            timeout_override_seconds=15.0,
        )

        # `grpc.aio` UnaryUnaryMultiCallable returns an `_AioCall` (awaitable
        # but not a coroutine), so wrap in a thin coroutine for create_task.
        async def _do_submit() -> approvals_pb2.SubmitDecision:
            return await req_stub.Submit(submit_req, metadata=auth_metadata)

        submit_task = asyncio.create_task(_do_submit())

        try:
            # Operator side: poll ListPending until our row shows up.
            target_id: str | None = None
            for _ in range(50):
                await asyncio.sleep(0.2)
                resp = await op_stub.ListPending(
                    approvals_pb2.ListPendingRequest(zone_id=zone),
                    metadata=auth_metadata,
                )
                rows = [
                    r for r in resp.requests if r.kind == "zone_access" and r.subject == subject
                ]
                if rows:
                    target_id = rows[0].id
                    break
            assert target_id is not None, (
                f"zone_access pending row never landed for zone={zone!r} subject={subject!r}"
            )

            decide_resp = await op_stub.Decide(
                approvals_pb2.DecideRequest(
                    request_id=target_id,
                    decision="approved",
                    scope="session",
                    reason="ok",
                ),
                metadata=auth_metadata,
            )
            assert decide_resp.id == target_id
            assert decide_resp.status == "approved"

            # Requester unblocks with approved. 5.0s is the generous E2E
            # ceiling — the dispatcher wakes the Submit waiter on the
            # same NotifyBridge channel the decide just published onto.
            result = await asyncio.wait_for(submit_task, 5.0)
            assert result.decision == "approved", (
                f"requester expected approved, got {result.decision!r} "
                f"(request_id={result.request_id})"
            )
            assert result.request_id

        finally:
            if not submit_task.done():
                submit_task.cancel()
                with contextlib.suppress(BaseException):
                    await submit_task
