"""E2E: Watch stream + diag dump against a real ``nexus up --build`` stack.

Issue #3790, Task 23. Two tests:

  1. ``test_watch_emits_pending_and_decided`` — opens a Watch stream on
     channel A, drives Submit + Decide on channel B, asserts the watcher
     receives BOTH a ``pending`` and a ``decided`` event. This is the
     exact event flow the operator UI consumes.

  2. ``test_diag_dump_returns_recent_pending`` — submits a row via gRPC,
     waits for it to land, then hits the read-only HTTP diag endpoint
     ``GET /hub/approvals/dump`` and asserts the row's subject is in the
     payload. The diag endpoint is what ops smoke-tests against in prod.

Both tests are grouped under one test class so the class-scoped
``running_nexus`` fixture spins up exactly one ``nexus up --build`` stack
for both tests, not two — module-level functions each get their own
implicit class under pytest's class scoping rules.

Diag auth contract
==================
``src/nexus/server/lifespan/approvals.py`` registers the diag router with
``allow_subject = os.environ.get("NEXUS_APPROVALS_DIAG_TOKEN") or None``.
When unset the lifespan disables ``GET /hub/approvals/dump`` entirely.
The ``running_nexus`` fixture sets ``NEXUS_APPROVALS_DIAG_TOKEN`` to a
generated secret and surfaces it as ``running_nexus.diag_token``. The
dump test sends ``Authorization: Bearer <diag_token>`` (#3790 round-13).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid

import grpc.aio
import httpx
import pytest

from nexus.grpc.approvals import approvals_pb2, approvals_pb2_grpc

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(600)]


def _tag() -> str:
    """uuid-prefixed identifier — keeps tests xdist-safe."""
    return uuid.uuid4().hex[:12]


class TestWatchAndDump:
    """Group Tasks 23's two tests so the class-scoped ``running_nexus``
    fixture is shared across both — without a class, scope="class" still
    yields per-test fixture instances since each bare-function test is
    its own implicit class.
    """

    @pytest.mark.asyncio
    async def test_watch_emits_pending_and_decided(self, running_nexus):
        """Watch on channel A receives BOTH pending + decided events for
        a Submit/Decide cycle driven from channels B + C.

        Three channels in play:
          - ch_watch: hosts the Watch stream subscription.
          - ch_submit: drives Submit (which blocks until decided, so it
            can't share with the decider).
          - ch_op: drives ListPending + Decide concurrently.
        """
        tag = _tag()
        zone = f"watch_{tag}"
        subject = f"watch.example:443:{tag}"
        auth_metadata: tuple = (("authorization", f"Bearer {running_nexus.admin_token}"),)

        async with grpc.aio.insecure_channel(running_nexus.grpc_addr) as ch_watch:
            watch_stub = approvals_pb2_grpc.ApprovalsV1Stub(ch_watch)

            events: list[approvals_pb2.ApprovalEvent] = []
            decided_seen = asyncio.Event()

            async def consume() -> None:
                call = watch_stub.Watch(
                    approvals_pb2.WatchRequest(zone_id=zone),
                    metadata=auth_metadata,
                )
                async for ev in call:
                    events.append(ev)
                    if ev.type == "decided":
                        decided_seen.set()
                        return

            watcher = asyncio.create_task(consume())

            try:
                # Give Watch a beat to register with the dispatcher
                # before we publish the pending row. Without this, fast
                # docker-local latency can produce the pending NOTIFY
                # before the watcher's subscription is wired up, and the
                # test misses the event.
                await asyncio.sleep(0.3)

                # ch_submit: drives Submit. Submit blocks until decide,
                # so we fire it on a task while ch_op decides.
                async with grpc.aio.insecure_channel(running_nexus.grpc_addr) as ch_submit:
                    submit_stub = approvals_pb2_grpc.ApprovalsV1Stub(ch_submit)
                    submit_req = approvals_pb2.SubmitRequest(
                        kind="egress_host",
                        subject=subject,
                        zone_id=zone,
                        token_id=f"tok_{tag}",
                        session_id=f"tok_{tag}:s",
                        agent_id=f"ag_{tag}",
                        reason="watch test",
                        metadata_json=json.dumps({}),
                        timeout_override_seconds=15.0,
                    )

                    async def _do_submit() -> approvals_pb2.SubmitDecision:
                        return await submit_stub.Submit(submit_req, metadata=auth_metadata)

                    submit_task = asyncio.create_task(_do_submit())

                    try:
                        # ch_op: the operator side. List + Decide.
                        async with grpc.aio.insecure_channel(running_nexus.grpc_addr) as ch_op:
                            op_stub = approvals_pb2_grpc.ApprovalsV1Stub(ch_op)
                            target_id: str | None = None
                            for _ in range(50):
                                await asyncio.sleep(0.2)
                                resp = await op_stub.ListPending(
                                    approvals_pb2.ListPendingRequest(zone_id=zone),
                                    metadata=auth_metadata,
                                )
                                rows = [r for r in resp.requests if r.subject == subject]
                                if rows:
                                    target_id = rows[0].id
                                    break
                            assert target_id is not None, (
                                f"pending row never landed for subject={subject!r}"
                            )
                            await op_stub.Decide(
                                approvals_pb2.DecideRequest(
                                    request_id=target_id,
                                    decision="approved",
                                    scope="once",
                                    reason="ok",
                                ),
                                metadata=auth_metadata,
                            )

                        # Submit unblocks once the decide fires.
                        await asyncio.wait_for(submit_task, 5.0)
                        # Watcher should see decided event — 5.0s is
                        # generous; NotifyBridge round-trips in ~10ms
                        # locally.
                        await asyncio.wait_for(decided_seen.wait(), 5.0)

                    finally:
                        if not submit_task.done():
                            submit_task.cancel()
                            with contextlib.suppress(BaseException):
                                await submit_task
            finally:
                if not watcher.done():
                    watcher.cancel()
                    with contextlib.suppress(BaseException):
                        await watcher

            types = [e.type for e in events]
            assert "pending" in types, f"no pending event — got {types}"
            assert "decided" in types, f"no decided event — got {types}"

    @pytest.mark.asyncio
    async def test_diag_dump_returns_recent_pending(self, running_nexus):
        """``GET /hub/approvals/dump?zone_id=<z>`` returns the pending row.

        Submit a request and leave it pending (don't decide it), then hit
        the diag endpoint. Assert the response payload contains an entry
        whose ``subject`` matches what we submitted.

        Auth: running_nexus sets NEXUS_APPROVALS_DIAG_TOKEN so the
        lifespan registers the diag router with an explicit allow_subject.
        The test sends ``Authorization: Bearer <diag_token>`` (#3790
        round-13 — unauthenticated access was removed).
        """
        tag = _tag()
        zone = f"dump_{tag}"
        subject = f"dump.example:443:{tag}"
        auth_metadata: tuple = (("authorization", f"Bearer {running_nexus.admin_token}"),)

        submit_task: asyncio.Task | None = None
        try:
            async with grpc.aio.insecure_channel(running_nexus.grpc_addr) as ch:
                stub = approvals_pb2_grpc.ApprovalsV1Stub(ch)

                submit_req = approvals_pb2.SubmitRequest(
                    kind="egress_host",
                    subject=subject,
                    zone_id=zone,
                    token_id=f"tok_{tag}",
                    session_id=f"tok_{tag}:s",
                    agent_id=f"ag_{tag}",
                    reason="dump test",
                    metadata_json=json.dumps({}),
                    timeout_override_seconds=15.0,
                )

                async def _do_submit() -> approvals_pb2.SubmitDecision:
                    return await stub.Submit(submit_req, metadata=auth_metadata)

                submit_task = asyncio.create_task(_do_submit())

                # Poll until pending lands (so the diag dump has
                # something to return). submit_task remains pending —
                # we're testing that the diag endpoint can see in-flight
                # rows.
                landed = False
                for _ in range(50):
                    await asyncio.sleep(0.2)
                    resp = await stub.ListPending(
                        approvals_pb2.ListPendingRequest(zone_id=zone),
                        metadata=auth_metadata,
                    )
                    if any(r.subject == subject for r in resp.requests):
                        landed = True
                        break
                assert landed, f"pending row never landed for subject={subject!r}"

                # Hit the diag dump. NEXUS_APPROVALS_DIAG_TOKEN is set in
                # the running_nexus fixture, so send the Bearer token.
                async with httpx.AsyncClient(
                    base_url=running_nexus.http_url, timeout=10.0
                ) as client:
                    r = await client.get(
                        f"/hub/approvals/dump?zone_id={zone}",
                        headers={"Authorization": f"Bearer {running_nexus.diag_token}"},
                    )
                assert r.status_code == 200, r.text
                payload = r.json()
                assert payload.get("pending"), (
                    f"dump endpoint returned no pending rows; payload={payload!r}"
                )
                assert any(p["subject"] == subject for p in payload["pending"]), (
                    f"dump payload missing subject={subject!r}; "
                    f"saw subjects={[p.get('subject') for p in payload['pending']]}"
                )
        finally:
            if submit_task is not None and not submit_task.done():
                submit_task.cancel()
                with contextlib.suppress(BaseException):
                    await submit_task
