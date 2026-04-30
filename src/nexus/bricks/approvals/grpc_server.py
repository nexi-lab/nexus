"""gRPC servicer for ApprovalsV1 — thin marshalling layer over ApprovalService."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Protocol

import grpc
import grpc.aio
from google.protobuf.timestamp_pb2 import Timestamp

from nexus.bricks.approvals.errors import ApprovalDenied, ApprovalTimeout, GatewayClosed
from nexus.bricks.approvals.models import (
    ApprovalKind,
    ApprovalRequest,
    Decision,
    DecisionScope,
    DecisionSource,
)
from nexus.bricks.approvals.service import ApprovalService
from nexus.grpc.approvals import approvals_pb2, approvals_pb2_grpc

logger = logging.getLogger(__name__)


class CapabilityAuth(Protocol):
    """ReBAC capability check.

    Implementations validate the request's caller against the named capability
    and return the caller's token id (used as `decided_by`/`token_id`).

    Two entry points are required:

      - ``authorize`` — abort the gRPC context on a denial (used when the
        caller supplies the zone, e.g. ListPending/Watch/Submit; surfacing
        PERMISSION_DENIED is fine because the caller already named the
        zone they want).
      - ``check_capability`` — return ``None`` on a ReBAC denial (used by
        Get/Decide/Cancel where the servicer fetches the row first and
        passes the row's zone_id; folding a denial into NOT_FOUND avoids
        leaking request_id existence across zones).

    Both still abort ``UNAUTHENTICATED`` on bad/missing tokens.
    """

    async def authorize(
        self,
        context: grpc.aio.ServicerContext,
        capability: str,
        zone_id: str,
    ) -> str: ...

    async def check_capability(
        self,
        context: grpc.aio.ServicerContext,
        capability: str,
        zone_id: str,
    ) -> str | None: ...


def _ts(d: datetime | None) -> Timestamp:
    ts = Timestamp()
    if d is not None:
        ts.FromDatetime(d)
    return ts


def _to_pb(req: ApprovalRequest) -> approvals_pb2.ApprovalRequestProto:
    return approvals_pb2.ApprovalRequestProto(
        id=req.id,
        zone_id=req.zone_id,
        kind=req.kind.value,
        subject=req.subject,
        agent_id=req.agent_id or "",
        token_id=req.token_id or "",
        session_id=req.session_id or "",
        reason=req.reason,
        metadata_json=json.dumps(req.metadata, default=str),
        status=req.status.value,
        created_at=_ts(req.created_at),
        expires_at=_ts(req.expires_at),
        decided_at=_ts(req.decided_at),
        decided_by=req.decided_by or "",
        decision_scope=req.decision_scope.value if req.decision_scope else "",
    )


class ApprovalsServicer(approvals_pb2_grpc.ApprovalsV1Servicer):
    """ApprovalsV1 gRPC servicer.

    Marshals proto <-> domain models and delegates to ApprovalService.
    Capability checks are delegated to a `CapabilityAuth` implementation.

    Per-zone authorization (Issue #3790, F1):
      - ``ListPending`` / ``Watch`` / ``Submit``: capability check uses
        the request's ``zone_id`` (rejected upstream if empty); a denial
        surfaces as PERMISSION_DENIED.
      - ``Get`` / ``Decide`` / ``Cancel``: row is fetched first and the
        capability check uses the row's ``zone_id``. A denial folds into
        NOT_FOUND so request_id existence does not leak across zones.
    """

    def __init__(self, service: ApprovalService, auth: CapabilityAuth) -> None:
        self._svc = service
        self._auth = auth

    async def ListPending(
        self,
        request: approvals_pb2.ListPendingRequest,
        context: grpc.aio.ServicerContext,
    ) -> approvals_pb2.ListPendingResponse:
        # Zone isolation (#3790): empty zone_id would leak across zones
        # when the ReBAC object is per-zone — reject before authorizing.
        if not request.zone_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "zone_id is required")
            raise  # unreachable; abort raises.
        await self._auth.authorize(context, "approvals:read", request.zone_id)
        rows = await self._svc.list_pending(zone_id=request.zone_id)
        return approvals_pb2.ListPendingResponse(requests=[_to_pb(r) for r in rows])

    async def Get(
        self,
        request: approvals_pb2.GetRequest,
        context: grpc.aio.ServicerContext,
    ) -> approvals_pb2.ApprovalRequestProto:
        # Fetch the row first so the capability check can be scoped to
        # the row's zone. Cross-zone callers (ones who lack the row's
        # zone-scoped capability) get NOT_FOUND so request_id existence
        # does not leak.
        row = await self._svc.get(request.request_id)
        if row is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "request not found")
            raise  # unreachable; abort raises. Keeps mypy happy on flow.
        subject = await self._auth.check_capability(context, "approvals:read", row.zone_id)
        if subject is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "request not found")
            raise  # unreachable.
        return _to_pb(row)

    async def Decide(
        self,
        request: approvals_pb2.DecideRequest,
        context: grpc.aio.ServicerContext,
    ) -> approvals_pb2.ApprovalRequestProto:
        # Fetch row first so the capability check is zone-scoped against
        # the row's zone_id, not a caller-supplied value.
        row = await self._svc.get(request.request_id)
        if row is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "request not found")
            raise  # unreachable.
        token_id = await self._auth.check_capability(context, "approvals:decide", row.zone_id)
        if token_id is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "request not found")
            raise  # unreachable.
        try:
            decision = Decision(request.decision)
            scope = DecisionScope(request.scope)
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "bad decision/scope")
            raise  # unreachable; abort raises. Keeps mypy happy on flow.
        try:
            row = await self._svc.decide(
                request_id=request.request_id,
                decision=decision,
                decided_by=token_id,
                scope=scope,
                reason=request.reason or None,
                source=DecisionSource.GRPC,
            )
        except ValueError as e:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(e))
            raise  # unreachable
        return _to_pb(row)

    async def Cancel(
        self,
        request: approvals_pb2.CancelRequest,
        context: grpc.aio.ServicerContext,
    ) -> approvals_pb2.CancelResponse:
        # Cancel today is a no-op for unknown ids; idempotent client
        # semantics. For known ids we still gate by the row's zone
        # capability (so a cross-zone caller can't probe for existence).
        row = await self._svc.get(request.request_id)
        if row is None:
            # Unknown id: idempotent OK. We don't run a zone-scoped
            # check here because there's no zone — and the response is
            # identical to "I cancelled it", so no leakage.
            logger.debug("approvals.cancel id=%s (unknown — no-op)", request.request_id)
            return approvals_pb2.CancelResponse()
        subject = await self._auth.check_capability(context, "approvals:decide", row.zone_id)
        if subject is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "request not found")
            raise  # unreachable.
        # Server-side Cancel is a no-op for known ids today; resolves to OK.
        # Logging the request_id keeps the field referenced and aids diagnostics.
        logger.debug("approvals.cancel id=%s zone=%s", request.request_id, row.zone_id)
        return approvals_pb2.CancelResponse()

    async def Watch(
        self,
        request: approvals_pb2.WatchRequest,
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[approvals_pb2.ApprovalEvent]:
        # Zone isolation (#3790) — see ListPending for rationale.
        if not request.zone_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "zone_id is required")
            raise  # unreachable.
        await self._auth.authorize(context, "approvals:read", request.zone_id)
        try:
            async for ev in self._svc.watch(zone_id=request.zone_id):
                yield approvals_pb2.ApprovalEvent(
                    type=ev.type,
                    request_id=ev.request_id,
                    zone_id=ev.zone_id,
                    decision=ev.decision or "",
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("approvals.watch stream errored")
            return

    async def Submit(
        self,
        request: approvals_pb2.SubmitRequest,
        context: grpc.aio.ServicerContext,
    ) -> approvals_pb2.SubmitDecision:
        if not request.zone_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "zone_id is required")
            raise  # unreachable.
        token_id = await self._auth.authorize(context, "approvals:request", request.zone_id)
        try:
            kind = ApprovalKind(request.kind)
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "bad kind")
            raise  # unreachable
        metadata: dict[str, Any] = {}
        if request.metadata_json:
            try:
                metadata = json.loads(request.metadata_json)
            except json.JSONDecodeError:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "bad metadata_json")
                raise  # unreachable
            if not isinstance(metadata, dict):
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "metadata_json must be an object",
                )
                raise  # unreachable
        # protobuf double defaults to 0.0; treat that as "no override".
        timeout = request.timeout_override_seconds or None
        request_id = f"req_push_{uuid.uuid4().hex[:12]}"
        # F1 (#3790): bind the session_id to the authenticated token_id so a
        # caller cannot replay a session_allow row written for a different
        # token. ``approval_session_allow`` is keyed on
        # ``(session_id, zone_id, kind, subject)``; without this prefix any
        # caller with ``approvals:request`` who knows or guesses another
        # caller's session_id could short-circuit operator decisions.
        # The client's value is preserved as a sub-component (so callers
        # can still correlate their own requests across calls) but the
        # ``grpc:{token_id}:`` namespace is forced server-side.
        if request.session_id:
            bound_session_id: str | None = f"grpc:{token_id}:{request.session_id}"
        else:
            bound_session_id = f"grpc:{token_id}"
        try:
            decision = await self._svc.request_and_wait(
                request_id=request_id,
                zone_id=request.zone_id,
                kind=kind,
                subject=request.subject,
                agent_id=request.agent_id or None,
                token_id=token_id,
                session_id=bound_session_id,
                reason=request.reason,
                metadata=metadata,
                timeout_override=timeout,
            )
        except ApprovalDenied:
            return approvals_pb2.SubmitDecision(decision="denied", request_id=request_id)
        except ApprovalTimeout as e:
            await context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, str(e))
            raise  # unreachable
        except GatewayClosed as e:
            await context.abort(grpc.StatusCode.UNAVAILABLE, str(e))
            raise  # unreachable
        return approvals_pb2.SubmitDecision(decision=decision.value, request_id=request_id)
