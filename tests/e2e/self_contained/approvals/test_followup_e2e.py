"""E2E: follow-ups A, B, C from Issue #3790 — against ``nexus up --build``.

Three tests grouped under a single class so the class-scoped
``running_nexus`` fixture spins up exactly ONE docker stack for the whole
suite (not three). Each test is xdist-safe via uuid-prefixed identifiers.

Follow-up coverage
==================

``test_a_mcp_mount_gate_round_trip``
    Follow-up A — drives a real unlisted-host SSE mount via
    ``POST /api/v2/mcp/mounts`` and proves the SSRFBlocked → PolicyGate →
    pending row → Decide → Submit round-trip all the way through the
    production code path. The HTTP request lands in
    ``MCPService.mcp_mount`` which calls ``MCPMountManager.mount`` and
    triggers ``_create_sse_client`` → ``validate_outbound_url`` raises
    ``SSRFBlocked`` (RFC1918 host) → ``_ssrf_blocked_via_gate`` consults
    the wired ``PolicyGate`` and submits an ``egress_host`` row. We
    poll for that row over gRPC and approve it; the mount-add HTTP call
    then unblocks. Outcome:

      - 201 if the operator approve unblocks the gate, the SSRF
        re-validation passes with ``allow_private=True``, AND the SSE
        handshake somehow succeeds (unlikely against a non-existent
        host, but possible if the docker network has a fake server).
      - 4xx/5xx (502/504/MCPMountError) when SSE handshake fails after
        gate approval — also acceptable. The gate path was exercised.

    KEY assertion: a pending ``egress_host`` row with subject=host:port
    landed in the approval queue. That proves the gate hook fired from
    a real production mount attempt, not just the gRPC contract.

``test_b_rebac_capability_auth_pipeline``
    Follow-up B — drives the FULL ReBACCapabilityAuth happy-path end-to-end
    against the running daemon. The fixture seeds both
    ``NEXUS_APPROVALS_ADMIN_TOKEN`` (gates the approvals fallback) and
    ``NEXUS_API_KEY`` (registered with the daemon's database during
    entrypoint bootstrap) so this test can mint a non-admin user key via
    ``POST /api/v2/auth/keys`` and have it recognised by the standard
    auth pipeline at gRPC time.

    Production wiring exercised:
      1. HTTP POST /api/v2/auth/keys (admin api-key) → AuthService key
         registry → returns a non-admin user API key.
      2. HTTP POST /api/v2/rebac/tuples (admin api-key) →
         ReBACManager.rebac_write → tuple persisted in postgres.
      3. gRPC ListPending with the non-admin user key →
         ReBACCapabilityAuth.authorize → AuthService.authenticate(token)
         resolves to the new user → rebac_check(("user", subject_id),
         "read", ("approvals", "global")) returns True via the granted
         viewer relation → ListPending returns 200.
      4. HTTP DELETE /api/v2/rebac/tuples (admin api-key) → tuple removed.
      5. gRPC ListPending with the same non-admin user key → ReBAC check
         returns False → AioRpcError(PERMISSION_DENIED).
      6. Pipeline checks: bogus bearer → UNAUTHENTICATED, missing
         bearer → UNAUTHENTICATED, admin bearer → ListPending OK.

    See ``src/nexus/bricks/approvals/grpc_auth.py`` for the capability-
    string -> permission mapping (``approvals:read`` -> ReBAC ``read``
    on ``("approvals", "global")``, granted by the ``viewer`` /
    ``direct_viewer`` / ``reader`` relations via
    ``RELATION_TO_PERMISSIONS`` in ``src/nexus/bricks/rebac/domain.py``).

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
        """Drive a real unlisted-host SSE mount through ``POST
        /api/v2/mcp/mounts`` and observe the gate path firing.

        Production wiring: HTTP → MCPService.mcp_mount → MCPMountManager
        → _create_sse_client → validate_outbound_url raises SSRFBlocked
        (RFC1918) → _ssrf_blocked_via_gate(gate) → PolicyGate.check →
        approvals.Submit → pending row → operator Decide → re-validate
        with allow_private=True → SSE handshake → 201 OR mount-error.

        ``mount.py`` constructs the gate request with:
          - kind=EGRESS_HOST
          - subject=host:port  (RFC1918 in this test)
          - zone_id=ROOT_ZONE_ID ("default")
          - session_id=None
          - token_id=mcp_mount:<mount_name>
          - agent_id=<mount_name>
          - reason="mcp_mount_connect"

        We POST the mount-add concurrently with a gRPC poll for the
        pending row, approve via Decide, and then await the mount-add
        response. The response itself may be 201 (unlikely — no SSE
        server listening at the private host) or 4xx/5xx (mount fails
        post-approval at SSE handshake). Either way the gate hook fired
        — that's the production gap we're closing.

        Auth: the new MCP router admits ``NEXUS_APPROVALS_ADMIN_TOKEN``
        as an admin-equivalent bearer (#3790 follow-up — same env var
        the gRPC server already trusts). The fixture sets it but does
        NOT seed ``NEXUS_API_KEY``, so the standard admin path is
        unavailable here.
        """
        tag = _tag()
        # ROOT_ZONE_ID is "root" — see nexus/contracts/constants.py:132.
        # mount.py:542 pins zone_id=ROOT_ZONE_ID for mount-time gate
        # consultations because there's no per-call zone at mount time.
        zone = "root"
        # Private RFC1918 host — validate_outbound_url(allow_private=False)
        # blocks it, which fires the gate. uuid-suffixed port to keep
        # concurrent xdist runs from colliding on the coalesce key.
        host = "10.0.0.99"
        # Use a tag-derived port in the high range so concurrent runs
        # have distinct (host, port) coalesce keys.
        port = 30000 + (int(tag, 16) % 30000)
        subject = f"{host}:{port}"
        mount_name = f"e2e-mount-{tag}"
        sse_url = f"http://{host}:{port}/sse"
        bearer = f"Bearer {running_nexus.admin_token}"
        auth_metadata: tuple = (("authorization", bearer),)
        http_headers = {"Authorization": bearer}

        # Smoke check: approvals stack is up.
        async with httpx.AsyncClient(timeout=5.0) as smoke:
            resp = await smoke.get(f"{running_nexus.http_url}/hub/approvals/dump")
            assert resp.status_code == 200, (
                f"approvals stack diag dump unhealthy: status={resp.status_code} "
                f"body={resp.text[:200]!r}"
            )

        # Generous timeout: SSE handshake against a black-hole RFC1918
        # host can take a while to fail. We bound it from outside.
        mount_client = httpx.AsyncClient(timeout=60.0)
        op_channel = grpc.aio.insecure_channel(running_nexus.grpc_addr)
        op_stub = approvals_pb2_grpc.ApprovalsV1Stub(op_channel)

        async def _do_post_mount() -> httpx.Response:
            return await mount_client.post(
                f"{running_nexus.http_url}/api/v2/mcp/mounts",
                headers=http_headers,
                json={
                    "name": mount_name,
                    "transport": "sse",
                    "url": sse_url,
                    "description": "e2e-followup-a — unlisted host gate trip",
                },
            )

        mount_task = asyncio.create_task(_do_post_mount())
        mount_succeeded = False

        try:
            # Poll the approvals queue for the pending row the gate
            # submits. This is the load-bearing assertion: a row only
            # appears if SSRFBlocked → _ssrf_blocked_via_gate → gate.check
            # → ApprovalService.request_and_wait → INSERT all fired.
            pending = await _wait_for_pending(
                op_stub,
                zone_id=zone,
                subject=subject,
                metadata=auth_metadata,
                deadline_seconds=15.0,
            )
            assert pending.kind == "egress_host", (
                f"expected kind=egress_host (mount-time gate), got {pending.kind!r}"
            )
            assert pending.zone_id == zone, (
                f"expected zone_id={zone!r} (ROOT_ZONE_ID), got {pending.zone_id!r}"
            )
            assert pending.subject == subject

            # Approve. The gate's check() will return APPROVED and the
            # mount manager re-validates with allow_private=True, then
            # tries the SSE handshake.
            decide_resp = await op_stub.Decide(
                approvals_pb2.DecideRequest(
                    request_id=pending.id,
                    decision="approved",
                    scope="session",
                    reason="e2e-followup-a-approve",
                ),
                metadata=auth_metadata,
            )
            assert decide_resp.status == "approved"

            # Wait for the mount-add HTTP response. It may succeed or
            # fail post-approval — both outcomes prove the gate fired.
            try:
                resp = await asyncio.wait_for(mount_task, timeout=45.0)
            except TimeoutError:
                # Acceptable: SSE handshake hung. The gate path fired
                # (we saw the pending row + approved), so the test goal
                # is met. Cancel and continue to teardown.
                pass
            else:
                # Any non-5xx-internal response is fine. We assert only
                # that a non-SSRF reason came back when failing.
                if resp.status_code == 201:
                    mount_succeeded = True
                    body = resp.json()
                    assert body.get("name") == mount_name
                else:
                    # Don't be picky about exact error code — SSE failures
                    # surface as 400/500 from MCPMountError. What we
                    # forbid is "SSRF still blocked", which would mean
                    # the gate wasn't consulted.
                    body_text = resp.text.lower()
                    assert "ssrf" not in body_text or "approved" in body_text, (
                        f"mount failed with SSRF-like error after approval: "
                        f"status={resp.status_code} body={resp.text[:300]!r}"
                    )

        finally:
            if not mount_task.done():
                mount_task.cancel()
                with contextlib.suppress(BaseException):
                    await mount_task
            await mount_client.aclose()
            try:
                if mount_succeeded:
                    # Best-effort cleanup: DELETE the mount we just
                    # created so subsequent tests in this class start
                    # from a clean state.
                    async with httpx.AsyncClient(timeout=10.0) as cleanup:
                        await cleanup.delete(
                            f"{running_nexus.http_url}/api/v2/mcp/mounts/{mount_name}",
                            headers=http_headers,
                        )
            finally:
                await op_channel.close()

    # ------------------------------------------------------------------
    # Follow-up B — ReBAC CapabilityAuth happy-path (deferred)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_b_rebac_capability_auth_pipeline(self, running_nexus) -> None:
        """Drive the FULL ReBACCapabilityAuth happy-path end-to-end.

        Phases:
          1. Mint a non-admin user API key via
             ``POST /api/v2/auth/keys`` (admin api-key bearer). This
             registers a real subject in the daemon's auth store that
             the standard auth pipeline (``AuthService.authenticate``)
             will resolve at gRPC time.
          2. Grant ``(user, <new-user-id>) -- viewer -> (approvals,
             global)`` via ``POST /api/v2/rebac/tuples``. The ``viewer``
             relation maps to ``read`` in
             ``RELATION_TO_PERMISSIONS``, which is exactly what
             ``ReBACCapabilityAuth.authorize(approvals:read)`` checks.
          3. Open a gRPC channel as the new non-admin user (raw API key
             passed as ``Authorization: Bearer <key>``) and call
             ``ListPending``. Must return success — proves the full
             pipeline accepted the user token, looked up the tuple,
             and granted the read capability.
          4. DELETE the tuple via ``/api/v2/rebac/tuples``.
          5. Re-call ``ListPending`` with the same user token. Must
             abort ``PERMISSION_DENIED`` — proves the ReBAC check is
             actually consulted on every request (not cached past
             tuple deletion in a way that masks revocation).
          6. Pipeline checks: bogus bearer → UNAUTHENTICATED, missing
             bearer → UNAUTHENTICATED, admin token → ListPending OK.
        """
        tag = _tag()
        zone = running_nexus.zone
        approvals_admin_bearer = f"Bearer {running_nexus.admin_token}"
        admin_api_key_bearer = f"Bearer {running_nexus.admin_api_key}"
        admin_api_key_headers = {"Authorization": admin_api_key_bearer}
        # Per-zone capability isolation (Issue #3790, F1): the ReBAC
        # object is ``("approvals", <zone>)``, not the flat
        # ``("approvals", "global")``. Grant on the same zone the
        # caller will pass to ListPending so the check passes.
        tuple_zone = zone
        keys_url = f"{running_nexus.http_url}/api/v2/auth/keys"
        rebac_url = f"{running_nexus.http_url}/api/v2/rebac/tuples"

        # ------------------------------------------------------------------
        # Phase 1 — mint a non-admin user API key as the test grantee.
        # ------------------------------------------------------------------
        grantee_user_id = f"e2e-grantee-{tag}"
        async with httpx.AsyncClient(timeout=15.0) as http:
            create_key_resp = await http.post(
                keys_url,
                headers=admin_api_key_headers,
                json={
                    "label": f"e2e-followup-b-{tag}",
                    "user_id": grantee_user_id,
                    "subject_type": "user",
                    "subject_id": grantee_user_id,
                    "zone_id": tuple_zone,
                    "is_admin": False,
                },
            )
            assert create_key_resp.status_code == 201, (
                f"POST /api/v2/auth/keys failed: status={create_key_resp.status_code} "
                f"body={create_key_resp.text[:400]!r}"
            )
            create_body = create_key_resp.json()
            user_api_key: str = create_body.get("key") or create_body.get("raw_key", "")
            assert user_api_key, f"create-key response missing key: {create_body!r}"
            user_key_id = create_body.get("key_id") or create_body.get("id")
            assert user_key_id, f"create-key response missing key_id: {create_body!r}"
            # Resolve the subject the daemon actually registered. Some setups
            # canonicalise user_id (e.g. lowercase) — use the response's
            # subject_id when present, falling back to user_id.
            registered_subject_id = (
                create_body.get("subject_id") or create_body.get("user_id") or grantee_user_id
            )

            tuple_body = {
                "subject_namespace": "user",
                "subject_id": registered_subject_id,
                "relation": "viewer",
                "object_namespace": "approvals",
                # F1: per-zone object — ListPending(zone=Z) requires
                # ``viewer`` on ``("approvals", Z)``.
                "object_id": tuple_zone,
                "zone_id": tuple_zone,
            }

            try:
                # ----------------------------------------------------------
                # Phase 2 — grant (user, registered_subject) viewer ->
                #           (approvals, global).
                # ----------------------------------------------------------
                grant_resp = await http.post(
                    rebac_url, headers=admin_api_key_headers, json=tuple_body
                )
                assert grant_resp.status_code == 201, (
                    f"POST /api/v2/rebac/tuples failed: status={grant_resp.status_code} "
                    f"body={grant_resp.text[:300]!r}"
                )
                grant_body = grant_resp.json()
                assert grant_body.get("tuple_id"), f"missing tuple_id in response: {grant_body!r}"

                # ----------------------------------------------------------
                # Phase 3 — non-admin user can ListPending via gRPC.
                # ----------------------------------------------------------
                user_metadata: tuple = (("authorization", f"Bearer {user_api_key}"),)
                async with grpc.aio.insecure_channel(running_nexus.grpc_addr) as user_channel:
                    user_stub = approvals_pb2_grpc.ApprovalsV1Stub(user_channel)
                    user_resp = await asyncio.wait_for(
                        user_stub.ListPending(
                            approvals_pb2.ListPendingRequest(zone_id=zone),
                            metadata=user_metadata,
                        ),
                        timeout=10.0,
                    )
                    assert hasattr(user_resp, "requests"), (
                        f"granted-user ListPending response missing `requests` field: {user_resp!r}"
                    )

                # ----------------------------------------------------------
                # Phase 4 — revoke the grant.
                # ----------------------------------------------------------
                del_resp = await http.request(
                    "DELETE", rebac_url, headers=admin_api_key_headers, json=tuple_body
                )
                assert del_resp.status_code == 200, (
                    f"DELETE /api/v2/rebac/tuples failed: status={del_resp.status_code} "
                    f"body={del_resp.text[:300]!r}"
                )
                assert del_resp.json()["deleted"] >= 1, (
                    f"DELETE reported zero deletions; expected >=1: {del_resp.json()!r}"
                )

                # ----------------------------------------------------------
                # Phase 5 — same user token must now be denied.
                # The auth pipeline still resolves the token to the
                # subject (the API key is not revoked), but the ReBAC
                # check now returns False -> PERMISSION_DENIED.
                # ----------------------------------------------------------
                async with grpc.aio.insecure_channel(running_nexus.grpc_addr) as denied_channel:
                    denied_stub = approvals_pb2_grpc.ApprovalsV1Stub(denied_channel)
                    with pytest.raises(grpc.aio.AioRpcError) as denied_info:
                        await asyncio.wait_for(
                            denied_stub.ListPending(
                                approvals_pb2.ListPendingRequest(zone_id=zone),
                                metadata=user_metadata,
                            ),
                            timeout=10.0,
                        )
                    assert denied_info.value.code() == grpc.StatusCode.PERMISSION_DENIED, (
                        f"expected PERMISSION_DENIED after grant revoke, got "
                        f"{denied_info.value.code()!r} "
                        f"details={denied_info.value.details()!r}"
                    )
            finally:
                # Best-effort cleanup: drop tuple + revoke the user key
                # so the daemon doesn't accumulate test droppings between
                # runs in the same docker stack lifetime.
                with contextlib.suppress(Exception):
                    await http.request(
                        "DELETE",
                        rebac_url,
                        headers=admin_api_key_headers,
                        json=tuple_body,
                    )
                with contextlib.suppress(Exception):
                    await http.delete(
                        f"{keys_url}/{user_key_id}",
                        headers=admin_api_key_headers,
                    )

        # ------------------------------------------------------------------
        # Phase 6 — gRPC bearer-token pipeline checks (existing).
        # ------------------------------------------------------------------
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
            assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED, (
                f"expected UNAUTHENTICATED for bogus bearer, got "
                f"{exc_info.value.code()!r} details={exc_info.value.details()!r}"
            )

        # Case 2: approvals admin bearer accepted (admin-fallback path),
        # ListPending returns a response.
        admin_metadata: tuple = (("authorization", approvals_admin_bearer),)
        async with grpc.aio.insecure_channel(running_nexus.grpc_addr) as admin_channel:
            admin_stub = approvals_pb2_grpc.ApprovalsV1Stub(admin_channel)
            resp = await asyncio.wait_for(
                admin_stub.ListPending(
                    approvals_pb2.ListPendingRequest(zone_id=zone),
                    metadata=admin_metadata,
                ),
                timeout=10.0,
            )
            assert hasattr(resp, "requests"), (
                f"admin ListPending response missing `requests` field: {resp!r}"
            )

        # Case 3 (no header): also UNAUTHENTICATED.
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
