"""E2E: MCP unlisted egress -> approve via gRPC -> tool call resolves.

Issue #3790, Task 21. Validates the full out-of-process gRPC path against
a real ``nexus up --build`` stack:

  - The daemon's lifespan starts a process-local ``grpc.aio.Server`` on
    ``NEXUS_APPROVALS_GRPC_PORT`` (default 2029) when both
    ``NEXUS_APPROVALS_ENABLED=1`` and ``NEXUS_APPROVALS_ADMIN_TOKEN`` are
    set in the container's environment.
  - docker-compose.yml / nexus-stack.yml forward both env vars and expose
    ``:2029`` on the host so this test can reach the gRPC server from
    outside the docker network.

Test-driven scenario
====================
At time of writing, ``MCPMountManager`` does not actually receive the
``policy_gate`` instance from ``connection_manager.py`` /
``mcp_service.py`` (see ``connection_manager.py:136`` and
``mcp_service.py:854`` — both construct ``MCPMountManager`` without the
``policy_gate=`` kwarg). So a true mount-time SSRF -> gate hop cannot
yet be triggered through the production daemon path. Per-request
``nexus_fetch`` egress also doesn't exist (Task 18 finding).

What we *can* exercise end-to-end is the gRPC pipeline that the MCP
egress hook would invoke once that wiring lands: an out-of-process
client opens a gRPC channel to the docker-forwarded ``:2029`` port,
calls ``Submit(kind=EGRESS_HOST, subject=<host:port>, ...)`` — which is
the exact RPC an MCP-egress hook would hit from a sidecar — and a second
out-of-process client (the "operator") calls
``ListPending`` -> ``Decide(approved)``. The Submit caller's request
unblocks with ``decision="approved"``, proving that:

  1. The lifespan actually started the gRPC server on the configured port.
  2. ``BearerTokenCapabilityAuth`` accepts the admin bearer.
  3. ``ApprovalService.request_and_wait`` round-trips through asyncpg
     LISTEN/NOTIFY (the dispatcher wakes the Submit waiter on the
     decide).
  4. The decoded ``Decision.APPROVED`` is the same value the MCP
     egress hook would return to the SSRF caller.

When the ``MCPMountManager`` policy_gate wiring lands (out of scope for
Task 21), this test stays valid as a contract test for the gRPC surface
itself; an additional test calling ``nexus mount add`` against a real
unlisted host can be layered on top.
"""

from __future__ import annotations

import asyncio
import uuid

import grpc.aio
import pytest

from nexus.grpc.approvals import approvals_pb2, approvals_pb2_grpc

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(600)]


def _tag() -> str:
    """uuid-prefixed identifier — keeps tests xdist-safe."""
    return uuid.uuid4().hex[:12]


async def _wait_for_pending(
    stub: approvals_pb2_grpc.ApprovalsV1Stub,
    *,
    zone_id: str,
    subject: str,
    metadata: tuple,
    deadline_seconds: float = 5.0,
) -> approvals_pb2.ApprovalRequestProto:
    """Poll ListPending until a row with the given subject lands.

    The Submit RPC generates the request_id internally so the operator
    side has to discover it. 5.0s is the generous E2E ceiling — under
    plain Postgres LISTEN/NOTIFY the row appears within ~10ms.
    """
    deadline = asyncio.get_event_loop().time() + deadline_seconds
    last_pending: list = []
    while asyncio.get_event_loop().time() < deadline:
        resp = await stub.ListPending(
            approvals_pb2.ListPendingRequest(zone_id=zone_id),
            metadata=metadata,
        )
        last_pending = list(resp.requests)
        match = [r for r in last_pending if r.subject == subject]
        if match:
            return match[0]
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"no pending request for subject={subject!r} in zone={zone_id!r} "
        f"after {deadline_seconds}s; last list: {[(r.subject, r.kind) for r in last_pending]}"
    )


@pytest.mark.asyncio
async def test_unlisted_host_pause_then_approve_unblocks_tool_call(running_nexus):
    """Out-of-process gRPC approve unblocks an MCP-egress-style request.

    Two coroutines, each on its own gRPC channel:

      - "tool_call": calls ``Submit`` with kind=EGRESS_HOST against an
        unlisted host. The RPC blocks until ApprovalService.decide
        flips the row to approved/denied. This is the same ``request_and_wait``
        path the MCP egress hook (Task 18) uses through PolicyGate.

      - "operator": polls ``ListPending``, finds the row by subject,
        calls ``Decide(approved, scope=session)``.

    Once the operator approves, the tool_call task receives
    ``SubmitDecision(decision="approved")``. The test asserts the value
    a real MCP egress hook would compare against — ``"approved"`` is the
    string form of ``Decision.APPROVED`` in the protobuf surface.
    """
    tag = _tag()
    # Subject mirrors what `MCPMountManager._ssrf_blocked_via_gate` uses
    # for unlisted hosts — ``host:port`` derived from the URL.
    subject = f"unlisted-host-{tag}.example.invalid:443"
    auth_metadata: tuple = (("authorization", f"Bearer {running_nexus.admin_token}"),)

    # ------------------------------------------------------------------
    # tool_call channel — owned by the "MCP sidecar" coroutine. We open
    # it before kicking off the task so the channel is ready when Submit
    # fires; the context manager closes it on exit, regardless of
    # decision outcome.
    # ------------------------------------------------------------------
    async with (
        grpc.aio.insecure_channel(running_nexus.grpc_addr) as tool_channel,
        grpc.aio.insecure_channel(running_nexus.grpc_addr) as op_channel,
    ):
        tool_stub = approvals_pb2_grpc.ApprovalsV1Stub(tool_channel)
        op_stub = approvals_pb2_grpc.ApprovalsV1Stub(op_channel)

        # Build the Submit request. metadata_json mirrors what the MCP
        # egress hook attaches in mount.py — operators can correlate
        # repeated requests for the same mount in the queue UI.
        submit_req = approvals_pb2.SubmitRequest(
            kind="egress_host",
            subject=subject,
            zone_id=running_nexus.zone,
            token_id=f"mcp_mount:e2e-{tag}",
            session_id=f"e2e-session-{tag}",
            agent_id=f"e2e-agent-{tag}",
            reason="mcp_mount_connect",
            metadata_json=(
                '{"url": "https://' + subject + '", '
                '"mount_name": "e2e-' + tag + '", '
                '"operation": "mcp_mount_connect"}'
            ),
            # 0.0 falls back to the brick's configured default; keep it
            # explicit so the test contract is obvious.
            timeout_override_seconds=0.0,
        )

        # `grpc.aio` UnaryUnaryMultiCallable returns an `_AioCall` (awaitable
        # but not a coroutine), so wrapping it in `asyncio.create_task`
        # directly fails on Python 3.14 with "a coroutine was expected".
        # Wrap the call in a thin coroutine so create_task is happy.
        async def _do_submit() -> approvals_pb2.SubmitDecision:
            return await tool_stub.Submit(submit_req, metadata=auth_metadata)

        tool_call_task = asyncio.create_task(_do_submit())

        try:
            # Operator side: poll ListPending until our row shows up,
            # then approve. 5.0s gives plenty of room under E2E latency
            # — local docker compose stacks usually surface the row in
            # well under a second.
            pending = await _wait_for_pending(
                op_stub,
                zone_id=running_nexus.zone,
                subject=subject,
                metadata=auth_metadata,
                deadline_seconds=5.0,
            )
            assert pending.kind == "egress_host"
            assert pending.zone_id == running_nexus.zone
            assert pending.subject == subject
            # ApprovalsServicer.Submit overwrites the request's token_id
            # with the auth-resolved caller (BearerTokenCapabilityAuth
            # returns ``admin:<first 8 chars>``). That's the contract — the
            # decided_by field on the row mirrors the same value.
            assert pending.token_id.startswith("admin:")

            decide_resp = await op_stub.Decide(
                approvals_pb2.DecideRequest(
                    request_id=pending.id,
                    decision="approved",
                    scope="session",
                    reason="e2e-test-approve",
                ),
                metadata=auth_metadata,
            )
            assert decide_resp.id == pending.id
            assert decide_resp.status == "approved"

            # Tool side resolves with SubmitDecision(approved). 5.0s is
            # the generous E2E ceiling; the dispatcher wakes the waiter
            # on the same NotifyBridge channel the decide just published
            # onto.
            decision = await asyncio.wait_for(tool_call_task, 5.0)
            assert decision.decision == "approved", (
                f"tool_call expected approved, got {decision.decision!r} "
                f"(request_id={decision.request_id})"
            )
            # request_id is server-generated; just assert it's non-empty
            # so the contract that callers can correlate is honored.
            assert decision.request_id

        finally:
            if not tool_call_task.done():
                tool_call_task.cancel()
                with __import__("contextlib").suppress(BaseException):
                    await tool_call_task
