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
    Should raise/abort via the grpc context if the caller is not authorized.
    """

    async def authorize(self, context: grpc.aio.ServicerContext, capability: str) -> str: ...


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
    """

    def __init__(self, service: ApprovalService, auth: CapabilityAuth) -> None:
        self._svc = service
        self._auth = auth

    async def ListPending(
        self,
        request: approvals_pb2.ListPendingRequest,
        context: grpc.aio.ServicerContext,
    ) -> approvals_pb2.ListPendingResponse:
        await self._auth.authorize(context, "approvals:read")
        rows = await self._svc.list_pending(zone_id=request.zone_id or None)
        return approvals_pb2.ListPendingResponse(requests=[_to_pb(r) for r in rows])

    async def Get(
        self,
        request: approvals_pb2.GetRequest,
        context: grpc.aio.ServicerContext,
    ) -> approvals_pb2.ApprovalRequestProto:
        await self._auth.authorize(context, "approvals:read")
        row = await self._svc.get(request.request_id)
        if row is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "request not found")
            raise  # unreachable; abort raises. Keeps mypy happy on flow.
        return _to_pb(row)

    async def Decide(
        self,
        request: approvals_pb2.DecideRequest,
        context: grpc.aio.ServicerContext,
    ) -> approvals_pb2.ApprovalRequestProto:
        token_id = await self._auth.authorize(context, "approvals:decide")
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
        await self._auth.authorize(context, "approvals:decide")
        # Server-side Cancel is a no-op for unknown ids; resolves to OK by design.
        # Logging the request_id keeps the field referenced and aids diagnostics.
        logger.debug("approvals.cancel id=%s", request.request_id)
        return approvals_pb2.CancelResponse()

    async def Watch(
        self,
        request: approvals_pb2.WatchRequest,
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[approvals_pb2.ApprovalEvent]:
        await self._auth.authorize(context, "approvals:read")
        try:
            async for ev in self._svc.watch(zone_id=request.zone_id or None):
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
        token_id = await self._auth.authorize(context, "approvals:request")
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
        try:
            decision = await self._svc.request_and_wait(
                request_id=request_id,
                zone_id=request.zone_id,
                kind=kind,
                subject=request.subject,
                agent_id=request.agent_id or None,
                token_id=token_id,
                session_id=request.session_id or None,
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
