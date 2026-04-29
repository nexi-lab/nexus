"""ApprovalService — async core."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from nexus.bricks.approvals.config import ApprovalConfig
from nexus.bricks.approvals.errors import (
    ApprovalDenied,
    ApprovalTimeout,
    GatewayClosed,
)
from nexus.bricks.approvals.events import Dispatcher, NotifyBridge
from nexus.bricks.approvals.models import (
    ApprovalKind,
    ApprovalRequest,
    ApprovalRequestStatus,
    Decision,
    DecisionScope,
    DecisionSource,
)
from nexus.bricks.approvals.repository import ApprovalRepository

logger = logging.getLogger(__name__)

CHANNEL_NEW = "approvals_new"
CHANNEL_DECIDED = "approvals_decided"


class ApprovalService:
    """Async core service for the approval queue.

    Wraps the repository with future-based waiting and Postgres LISTEN/NOTIFY
    cross-worker coordination. Each instance maintains an in-process dispatcher
    of futures keyed by request_id; decisions resolve them; a NOTIFY arriving
    from any worker also resolves any matching future locally.
    """

    def __init__(
        self,
        repository: ApprovalRepository,
        notify_bridge: NotifyBridge,
        config: ApprovalConfig,
    ) -> None:
        self._repo = repository
        self._notify = notify_bridge
        self._cfg = config
        self._dispatcher = Dispatcher()

    @property
    def repository(self) -> ApprovalRepository:
        """Public accessor — used by PolicyGate (Task 13) for session_allow."""
        return self._repo

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        await self._notify.start(
            {
                CHANNEL_DECIDED: self._on_decided_payload,
                CHANNEL_NEW: self._on_new_payload,
            }
        )

    async def stop(self) -> None:
        await self._notify.stop()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def request_and_wait(
        self,
        *,
        request_id: str,
        zone_id: str,
        kind: ApprovalKind,
        subject: str,
        agent_id: str | None,
        token_id: str | None,
        session_id: str | None,
        reason: str,
        metadata: dict[str, Any],
        timeout_override: float | None = None,
    ) -> Decision:
        timeout = self._cfg.clamp_request_timeout(timeout_override)
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=timeout)

        try:
            req = await self._repo.insert_or_fetch_pending(
                request_id=request_id,
                zone_id=zone_id,
                kind=kind,
                subject=subject,
                agent_id=agent_id,
                token_id=token_id,
                session_id=session_id,
                reason=reason,
                metadata=metadata,
                now=now,
                expires_at=expires,
            )
        except Exception as e:
            raise GatewayClosed("could not insert pending row") from e

        # Was it newly inserted under our id, or an existing coalesced row?
        if req.id == request_id:
            try:
                await self._notify.notify(
                    CHANNEL_NEW,
                    json.dumps({"request_id": req.id, "zone_id": zone_id}),
                )
            except Exception:
                logger.warning("notify(approvals_new) failed; queue still durable", exc_info=True)

        fut = self._dispatcher.register(req.id)

        # If the row is already terminal (race: decided between insert and register),
        # short-circuit by re-fetching.
        latest = await self._repo.get(req.id)
        if latest and latest.status is not ApprovalRequestStatus.PENDING:
            self._dispatcher.cancel(fut)
            return _row_to_decision(latest, timeout=timeout)

        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError as e:
            self._dispatcher.cancel(fut)
            raise ApprovalTimeout(req.id, timeout) from e

        if result is Decision.DENIED:
            row = await self._repo.get(req.id)
            reason_str = row.decided_by if (row and row.decided_by) else "denied"
            raise ApprovalDenied(req.id, reason_str)
        return result

    async def decide(
        self,
        *,
        request_id: str,
        decision: Decision,
        decided_by: str,
        scope: DecisionScope,
        reason: str | None,
        source: DecisionSource,
    ) -> ApprovalRequest:
        new_status = (
            ApprovalRequestStatus.APPROVED
            if decision is Decision.APPROVED
            else ApprovalRequestStatus.REJECTED
        )
        now = datetime.now(UTC)

        updated = await self._repo.transition(
            request_id=request_id,
            new_status=new_status,
            decided_by=decided_by,
            scope=scope,
            reason=reason,
            source=source,
            now=now,
        )
        if updated is None:
            raise ValueError(f"request {request_id} is not pending")

        if scope is DecisionScope.SESSION and decision is Decision.APPROVED and updated.session_id:
            await self._repo.insert_session_allow(
                session_id=updated.session_id,
                zone_id=updated.zone_id,
                kind=updated.kind,
                subject=updated.subject,
                decided_by=decided_by,
                decided_at=now,
                request_id=updated.id,
            )

        await self._notify.notify(
            CHANNEL_DECIDED,
            json.dumps({"request_id": request_id, "decision": decision.value}),
        )
        # Resolve in-process futures immediately for callers on the same worker.
        self._dispatcher.resolve(request_id, decision)
        return updated

    async def list_pending(self, zone_id: str | None) -> list[ApprovalRequest]:
        return await self._repo.list_pending(zone_id)

    async def get(self, request_id: str) -> ApprovalRequest | None:
        return await self._repo.get(request_id)

    async def cancel(self, future: asyncio.Future[Decision]) -> None:
        self._dispatcher.cancel(future)

    # ------------------------------------------------------------------
    # NOTIFY handlers
    # ------------------------------------------------------------------

    async def _on_decided_payload(self, payload: str) -> None:
        try:
            msg = json.loads(payload)
            rid = msg["request_id"]
            decision = Decision(msg["decision"])
        except Exception:
            logger.warning("bad approvals_decided payload: %s", payload)
            return
        self._dispatcher.resolve(rid, decision)

    async def _on_new_payload(self, payload: str) -> None:
        # No-op for callers; only Watch-stream needs new-pending events (Task 10).
        pass


def _row_to_decision(row: ApprovalRequest, *, timeout: float) -> Decision:
    if row.status is ApprovalRequestStatus.APPROVED:
        return Decision.APPROVED
    if row.status is ApprovalRequestStatus.REJECTED:
        raise ApprovalDenied(row.id, "rejected")
    if row.status is ApprovalRequestStatus.EXPIRED:
        raise ApprovalTimeout(row.id, timeout)
    raise RuntimeError(f"unexpected status {row.status}")
