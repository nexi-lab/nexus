"""E2E: follow-ups A, B, C from Issue #3790 — against ``nexus up --build``.

Three tests grouped under a single class so the class-scoped
``running_nexus`` fixture spins up exactly ONE docker stack for the whole
suite (not three). Each test is xdist-safe via uuid-prefixed identifiers.

Follow-up coverage
==================

``test_a_mcp_mount_gate_round_trip``
    Follow-up A — proves the policy_gate threading wired the gate into
    the daemon's MCP service. The full SSRFBlocked → gate → Submit path
    runs entirely in-process inside the daemon container; we cannot
    drive ``nexus mcp mount add`` from the test environment because no
    HTTP/CLI mount-add endpoint for SSE MCP servers exists today (the
    closest, ``POST /api/v2/connectors/.../mounts``, mounts CONNECTORS
    not MCP servers). What we *can* prove out-of-process is that the
    exact gRPC surface the wired gate uses is operational under the
    private-host subject shape ``_ssrf_blocked_via_gate`` produces —
    same kind, same zone (``ROOT_ZONE_ID``), same subject convention
    (``host:port``), session_id=None, and the synthesized
    ``mcp_mount:<name>`` token_id.

    The wiring itself is verified indirectly: lifespan startup runs
    ``_wire_policy_gate_into_mcp`` synchronously after the gRPC server
    binds — so the test's ability to round-trip through gRPC implies
    ``set_policy_gate(stack.gate)`` returned successfully. Logs surface
    the exact line ``[APPROVALS] PolicyGate wired into MCPService`` at
    INFO; a future test running ``docker compose logs nexus`` could
    grep for it explicitly.

``test_b_rebac_capability_auth_pipeline``
    Follow-up B — proves the gRPC bearer-token pipeline rejects garbage
    and accepts the configured admin token. The full ReBAC happy-path
    (token issued through ``POST /api/v2/auth/keys`` + tuple grant
    ``(user, X) -- read --> (approvals, global)``) requires a generic
    HTTP ReBAC tuple-grant endpoint that does not exist in the current
    codebase (``auth_keys.py:_create_key_grants`` writes
    file-scoped tuples only — direct_viewer/editor/owner on
    ``("file", path)`` — not ``read`` on ``("approvals", "global")``).

    Per the task's deferred-fallback contract this stripped-down
    version asserts what the auth pipeline DOES guarantee end-to-end:
    a malformed bearer token is rejected ``UNAUTHENTICATED`` and the
    admin token is accepted. The full ReBAC tuple-grant happy-path is
    deferred — once a generic tuple-grant HTTP endpoint lands (or once
    a ``rebac_grant`` CLI command is added), this test should grow a
    second case that creates a non-admin token, grants
    ``approvals:read``, and asserts ListPending succeeds. See the
    inline comment for the exact tuple shape ReBACCapabilityAuth
    expects (``_CAPABILITY_TO_PERMISSION`` in
    ``src/nexus/bricks/approvals/grpc_auth.py``).

``test_c_late_insert_race_auto_inherit``
    Follow-up C — proves the recent-decision inherit fires under
    realistic race conditions through the real Postgres NOTIFY
    pipeline. Three callers:

      A) Submits, blocks, gets approved by the operator.
      B) Within the 2.0s grace window, submits the same (zone, kind,
         subject) with a DIFFERENT session_id (so the SESSION-scoped
         allow row doesn't short-circuit). Expected: Submit returns
         immediately with ``decision="approved"`` via auto-inherit.
      C) Outside the grace window (~3s later), submits again. Expected:
         a fresh pending row lands and B does NOT auto-inherit; the
         test cancels C's task without a Decide.

    All three callers exercise: NOTIFY round-trip + dispatcher fan-out
    + recent-decision query + transition + session_allow propagation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid

import grpc
import grpc.aio
import httpx
import pytest

from nexus.grpc.approvals import approvals_pb2, approvals_pb2_grpc

pytestmark = pytest.mark.e2e


def _tag() -> str:
    """uuid-prefixed identifier — keeps tests xdist-safe."""
    return uuid.uuid4().hex[:12]


async def _wait_for_pending(
    stub: approvals_pb2_grpc.ApprovalsV1Stub,
    *,
    zone_id: str,
    subject: str,
    metadata: tuple,
    deadline_seconds: float = 10.0,
) -> approvals_pb2.ApprovalRequestProto:
    """Poll ListPending until a row with the given subject lands.

    10.0s is the generous E2E ceiling — under plain Postgres LISTEN/NOTIFY
    the row appears within ~10ms, but the docker stack adds a small extra
    fixed cost.
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
        f"after {deadline_seconds}s; last list: "
        f"{[(r.subject, r.kind) for r in last_pending]}"
    )


class TestFollowupE2E:
    """Group all three follow-up tests so the class-scoped ``running_nexus``
    fixture spins up exactly ONE docker stack for the whole suite.
    """

    # ------------------------------------------------------------------
    # Follow-up A — MCP mount-time gate round-trip
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_a_mcp_mount_gate_round_trip(self, running_nexus) -> None:
        """Mirror what ``MCPMountManager._ssrf_blocked_via_gate`` submits.

        ``mount.py`` constructs the gate request with:
          - kind=EGRESS_HOST
          - subject=host:port  (RFC1918 in this test, since SSRF blocks
            those by default and that's exactly the path the gate exists
            to recover)
          - zone_id=ROOT_ZONE_ID  (mount-time has no per-call zone)
          - session_id=None  (mount-time has no session)
          - token_id=mcp_mount:<mount_name>
          - agent_id=<mount_name>
          - reason="mcp_mount_connect"

        We submit the same shape on one channel and approve on a second
        channel. A successful approve unblocks Submit with
        ``decision="approved"`` — proving the gRPC surface the wired
        PolicyGate hops through is operational. The SSE-handshake step
        downstream is out of scope (no SSE server inside the docker net
        for the private host); operators see the queued request and can
        approve it just as they would for a real mount attempt.

        Wiring assertion (indirect): the lifespan registers the gRPC
        server AFTER ``_wire_policy_gate_into_mcp(app, stack.gate)``,
        so the channel reaching the servicer at all proves the wiring
        line ran without raising. We also fetch /hub/approvals/dump
        as a positive smoke check that the approvals stack started.
        """
        tag = _tag()
        # ROOT_ZONE_ID is "default" today (see nexus/contracts/constants.py)
        # and the mount-time hop pins zone_id there. Mirror it so this
        # test exactly tracks the production path. We don't re-derive the
        # constant from the daemon — it's stable contract.
        zone = "default"
        # Private RFC1918 host:port — exactly what
        # validate_outbound_url(allow_private=False) blocks and what
        # _ssrf_blocked_via_gate then submits as the EGRESS_HOST subject.
        # uuid-suffixed so concurrent xdist runs don't collide on the
        # coalesce key.
        subject = f"10.0.0.99:9999:mount-{tag}"
        mount_name = f"e2e-mount-{tag}"
        auth_metadata: tuple = (("authorization", f"Bearer {running_nexus.admin_token}"),)

        # Smoke check: approvals stack is up (proves lifespan ran past
        # the wiring line).
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.get(f"{running_nexus.http_url}/hub/approvals/dump")
            assert resp.status_code == 200, (
                f"approvals stack diag dump unhealthy: status={resp.status_code} "
                f"body={resp.text[:200]!r}"
            )

        async with (
            grpc.aio.insecure_channel(running_nexus.grpc_addr) as gate_channel,
            grpc.aio.insecure_channel(running_nexus.grpc_addr) as op_channel,
        ):
            gate_stub = approvals_pb2_grpc.ApprovalsV1Stub(gate_channel)
            op_stub = approvals_pb2_grpc.ApprovalsV1Stub(op_channel)

            submit_req = approvals_pb2.SubmitRequest(
                kind="egress_host",
                subject=subject,
                zone_id=zone,
                token_id=f"mcp_mount:{mount_name}",
                session_id="",  # protobuf empty == None on the wire
                agent_id=mount_name,
                reason="mcp_mount_connect",
                metadata_json=json.dumps(
                    {
                        "url": f"http://{subject}/sse",
                        "mount_name": mount_name,
                        "operation": "mcp_mount_connect",
                    }
                ),
                timeout_override_seconds=15.0,
            )

            async def _do_submit() -> approvals_pb2.SubmitDecision:
                return await gate_stub.Submit(submit_req, metadata=auth_metadata)

            gate_task = asyncio.create_task(_do_submit())

            try:
                pending = await _wait_for_pending(
                    op_stub,
                    zone_id=zone,
                    subject=subject,
                    metadata=auth_metadata,
                    deadline_seconds=10.0,
                )
                assert pending.kind == "egress_host"
                assert pending.zone_id == zone
                assert pending.subject == subject
                # ApprovalsServicer.Submit overwrites token_id with the
                # auth-resolved caller; under the admin-token shim it's
                # ``admin:<first 8>``.
                assert pending.token_id.startswith("admin:")

                decide_resp = await op_stub.Decide(
                    approvals_pb2.DecideRequest(
                        request_id=pending.id,
                        decision="approved",
                        scope="session",
                        reason="e2e-followup-a-approve",
                    ),
                    metadata=auth_metadata,
                )
                assert decide_resp.id == pending.id
                assert decide_resp.status == "approved"

                decision = await asyncio.wait_for(gate_task, 10.0)
                assert decision.decision == "approved", (
                    f"gate-side expected approved, got {decision.decision!r} "
                    f"(request_id={decision.request_id})"
                )
                assert decision.request_id

            finally:
                if not gate_task.done():
                    gate_task.cancel()
                    with contextlib.suppress(BaseException):
                        await gate_task

    # ------------------------------------------------------------------
    # Follow-up B — ReBAC CapabilityAuth happy-path (deferred)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_b_rebac_capability_auth_pipeline(self, running_nexus) -> None:
        """Stripped-down auth-pipeline check for ReBACCapabilityAuth.

        DEFERRED: full ReBAC happy-path requires a tuple-grant endpoint
        that doesn't exist today (see module docstring). This test
        instead asserts the two ends of the pipeline that ARE provable
        from outside:

          1. A malformed/unknown bearer token is rejected with
             ``UNAUTHENTICATED`` (the ReBACCapabilityAuth fallback chain
             — ``authenticate() raises -> result is None -> admin
             fallback -> rejected -> abort UNAUTHENTICATED``).
          2. The admin token is accepted and ListPending returns a
             ListPendingResponse (possibly empty). This proves the
             admin-fallback branch of ReBACCapabilityAuth is wired
             when the daemon has both ReBACManager and the env-var
             admin token configured.

        The interesting middle case — a non-admin token whose subject_id
        has been granted ``read`` on ``("approvals", "global")`` via a
        ReBAC tuple — is NOT covered here. To extend this test once a
        tuple-grant API exists:

          - POST a non-admin key via /api/v2/auth/keys (no grants).
          - Issue a generic ReBAC tuple
            ``(<subject_type>, <subject_id>) -- read -> (approvals, global)``
            via the future endpoint.
          - Open a third gRPC channel with that token.
          - Assert ListPending succeeds and returns a response shaped
            like the admin path.

        See ``src/nexus/bricks/approvals/grpc_auth.py`` for the exact
        capability-string -> permission mapping the future test must use.
        """
        zone = running_nexus.zone

        # Case 1: garbage bearer rejected with UNAUTHENTICATED.
        bad_metadata: tuple = (("authorization", "Bearer not-a-real-token-deadbeef"),)
        async with grpc.aio.insecure_channel(running_nexus.grpc_addr) as bad_channel:
            bad_stub = approvals_pb2_grpc.ApprovalsV1Stub(bad_channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await asyncio.wait_for(
                    bad_stub.ListPending(
                        approvals_pb2.ListPendingRequest(zone_id=zone),
                        metadata=bad_metadata,
                    ),
                    timeout=10.0,
                )
            # ReBACCapabilityAuth + admin-fallback wraps unresolved
            # tokens through the fallback before aborting; both paths
            # land on UNAUTHENTICATED. PERMISSION_DENIED would mean the
            # subject resolved but the ReBAC check rejected — that's a
            # different failure mode, and we shouldn't accept it here.
            assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED, (
                f"expected UNAUTHENTICATED for bogus bearer, got "
                f"{exc_info.value.code()!r} details={exc_info.value.details()!r}"
            )

        # Case 2: admin bearer accepted, ListPending returns a response.
        admin_metadata: tuple = (("authorization", f"Bearer {running_nexus.admin_token}"),)
        async with grpc.aio.insecure_channel(running_nexus.grpc_addr) as admin_channel:
            admin_stub = approvals_pb2_grpc.ApprovalsV1Stub(admin_channel)
            resp = await asyncio.wait_for(
                admin_stub.ListPending(
                    approvals_pb2.ListPendingRequest(zone_id=zone),
                    metadata=admin_metadata,
                ),
                timeout=10.0,
            )
            # The response shape is what matters here — `requests` may
            # be empty (no rows seeded for this fresh zone yet) but the
            # field must exist as an iterable proto repeated.
            assert hasattr(resp, "requests"), (
                f"admin ListPending response missing `requests` field: {resp!r}"
            )

        # Case 3 (no header): also UNAUTHENTICATED. Catches the
        # `metadata is None` branch in BearerTokenCapabilityAuth /
        # _extract_bearer_token.
        async with grpc.aio.insecure_channel(running_nexus.grpc_addr) as no_auth_channel:
            no_auth_stub = approvals_pb2_grpc.ApprovalsV1Stub(no_auth_channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await asyncio.wait_for(
                    no_auth_stub.ListPending(
                        approvals_pb2.ListPendingRequest(zone_id=zone),
                        metadata=(),
                    ),
                    timeout=10.0,
                )
            assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED, (
                f"expected UNAUTHENTICATED for missing bearer, got {exc_info.value.code()!r}"
            )

    # ------------------------------------------------------------------
    # Follow-up C — late-insert race auto-inherit
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_c_late_insert_race_auto_inherit(self, running_nexus) -> None:
        """Three-caller race that exercises ``_maybe_inherit_recent_decision``.

        Sequence (all on real Postgres NOTIFY through the docker stack):

          A) Submit(kind=egress_host, subject, session_id=A_session).
             Block. Operator approves with scope=SESSION.
          B) Within 2.0s of A's decide, Submit same (zone, kind,
             subject) with session_id=B_session (different — so the
             session_allow row from A's SESSION-scope decide doesn't
             short-circuit B at the top of request_and_wait).
             Expected: ``_maybe_inherit_recent_decision`` runs in B's
             request_and_wait, finds A's terminal APPROVED row, and
             flips B's freshly-inserted PENDING row to APPROVED via
             ``transition``. B's Submit returns ``approved`` with NO
             second Decide call.
          C) Wait the grace window out (~3s after A's decide), then
             Submit a third time with session_id=C_session. Expected: B
             cleared the (zone, kind, subject) coalesce key when its
             row flipped APPROVED, so C inserts fresh PENDING. The
             ``get_recent_decision`` query still returns A's row but
             now it's outside ``since=now-2.0s`` so the inherit
             SHORTS-CIRCUITS to None. C blocks on operator input — we
             prove this by waiting 1.5s and asserting the task is still
             running, then cancel.

        Verification it really was inherit (not session_allow): each
        caller uses a DIFFERENT session_id, so the
        ``session_allow_exists`` short-circuit at line 137 of service.py
        cannot fire. The only path that can return APPROVED without an
        operator Decide is ``_maybe_inherit_recent_decision``.
        """
        tag = _tag()
        zone = f"race-zone-{tag}"
        # Subject is the coalesce key — must be identical across A, B, C
        # for the inherit logic to consider them "the same request".
        subject = f"race.example:443:{tag}"
        a_session = f"a-session-{tag}"
        b_session = f"b-session-{tag}"
        c_session = f"c-session-{tag}"
        auth_metadata: tuple = (("authorization", f"Bearer {running_nexus.admin_token}"),)

        def _submit_req(session_id: str) -> approvals_pb2.SubmitRequest:
            return approvals_pb2.SubmitRequest(
                kind="egress_host",
                subject=subject,
                zone_id=zone,
                token_id=f"tok-race-{tag}",
                session_id=session_id,
                agent_id=f"agent-race-{tag}",
                reason="e2e-followup-c-race",
                metadata_json=json.dumps({"session": session_id}),
                # Generous so the test isn't flaky if the operator
                # decide lags behind (15s gives us 12s after the wait).
                timeout_override_seconds=15.0,
            )

        async with (
            grpc.aio.insecure_channel(running_nexus.grpc_addr) as ch_a,
            grpc.aio.insecure_channel(running_nexus.grpc_addr) as ch_b,
            grpc.aio.insecure_channel(running_nexus.grpc_addr) as ch_c,
            grpc.aio.insecure_channel(running_nexus.grpc_addr) as ch_op,
        ):
            stub_a = approvals_pb2_grpc.ApprovalsV1Stub(ch_a)
            stub_b = approvals_pb2_grpc.ApprovalsV1Stub(ch_b)
            stub_c = approvals_pb2_grpc.ApprovalsV1Stub(ch_c)
            stub_op = approvals_pb2_grpc.ApprovalsV1Stub(ch_op)

            # ---- Caller A: submit + wait for operator approve. -------
            async def _submit_a() -> approvals_pb2.SubmitDecision:
                return await stub_a.Submit(_submit_req(a_session), metadata=auth_metadata)

            task_a = asyncio.create_task(_submit_a())
            task_b: asyncio.Task | None = None
            task_c: asyncio.Task | None = None

            try:
                pending_a = await _wait_for_pending(
                    stub_op,
                    zone_id=zone,
                    subject=subject,
                    metadata=auth_metadata,
                    deadline_seconds=10.0,
                )

                decide_resp = await stub_op.Decide(
                    approvals_pb2.DecideRequest(
                        request_id=pending_a.id,
                        decision="approved",
                        scope="session",
                        reason="approve-a",
                    ),
                    metadata=auth_metadata,
                )
                assert decide_resp.status == "approved"

                # Capture the wall-clock decide time so we can pace C
                # outside the 2.0s grace window relative to it. The
                # service grace check uses ``decided_at >= now - 2.0s``
                # at B/C's insert time, so C must fire later than
                # decide+2s to fall outside.
                decide_wall = time.monotonic()

                decision_a = await asyncio.wait_for(task_a, 10.0)
                assert decision_a.decision == "approved", (
                    f"A expected approved, got {decision_a.decision!r}"
                )

                # ---- Caller B: within grace window, expect inherit. --
                # The inherit check uses since=now - 2.0s, so as long as
                # B's insert happens within ~2s of A's decided_at the
                # recent-decision query finds A's row.
                async def _submit_b() -> approvals_pb2.SubmitDecision:
                    return await stub_b.Submit(_submit_req(b_session), metadata=auth_metadata)

                task_b = asyncio.create_task(_submit_b())
                decision_b = await asyncio.wait_for(task_b, 10.0)
                assert decision_b.decision == "approved", (
                    f"B expected auto-inherited approved, got "
                    f"{decision_b.decision!r} (request_id={decision_b.request_id})"
                )
                assert decision_b.request_id != pending_a.id, (
                    "B should have its OWN request_id (fresh insert) — "
                    "got the same id as A, which means the queue "
                    "returned A's record instead of inserting + "
                    "inheriting."
                )

                # Sanity: no extra pending row remained after B (the
                # transition flipped B's row to APPROVED, so the
                # pending-coalesce key is free again).
                resp = await stub_op.ListPending(
                    approvals_pb2.ListPendingRequest(zone_id=zone),
                    metadata=auth_metadata,
                )
                pending_after_b = [r for r in resp.requests if r.subject == subject]
                assert pending_after_b == [], (
                    f"unexpected pending rows after B inherit: "
                    f"{[(r.id, r.status) for r in pending_after_b]}"
                )

                # ---- Caller C: outside grace window, no inherit. -----
                # Sleep until at least 2.5s past the decide. If we just
                # awaited decision_b serially, only ~tens of ms have
                # elapsed since decide_wall.
                grace_window_seconds = 2.0
                target_post_decide = grace_window_seconds + 1.0  # 3.0s buffer
                elapsed = time.monotonic() - decide_wall
                if elapsed < target_post_decide:
                    await asyncio.sleep(target_post_decide - elapsed)

                async def _submit_c() -> approvals_pb2.SubmitDecision:
                    return await stub_c.Submit(_submit_req(c_session), metadata=auth_metadata)

                task_c = asyncio.create_task(_submit_c())

                # Wait until the new pending row lands (proves C did
                # NOT auto-inherit — if it had, task_c would already be
                # done and no pending row would appear).
                pending_c = await _wait_for_pending(
                    stub_op,
                    zone_id=zone,
                    subject=subject,
                    metadata=auth_metadata,
                    deadline_seconds=10.0,
                )
                assert pending_c.id != pending_a.id, (
                    "C's pending row id collided with A's; the "
                    "ListPending poll must be returning a stale row."
                )

                # Give the dispatcher a beat to verify C is genuinely
                # blocked (no operator action means the pending row
                # stays pending and task_c stays awaiting).
                await asyncio.sleep(0.5)
                assert not task_c.done(), (
                    "C unexpectedly resolved without operator Decide — "
                    "auto-inherit fired outside the grace window."
                )

            finally:
                # Cancel any still-running tasks in reverse-create order
                # so we leave no zombie coroutines behind.
                for task in (task_c, task_b, task_a):
                    if task is not None and not task.done():
                        task.cancel()
                        with contextlib.suppress(BaseException):
                            await task
