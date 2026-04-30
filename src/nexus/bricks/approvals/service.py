"""ApprovalService — async core."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
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
from nexus.bricks.approvals.sweeper import Sweeper

logger = logging.getLogger(__name__)

CHANNEL_NEW = "approvals_new"
CHANNEL_DECIDED = "approvals_decided"

# Grace window for inheriting a recent terminal decision when a late-inserted
# pending row appears for the same coalesce key. The partial unique index
# `approval_requests_pending_coalesce` is `WHERE status='pending'`, so once a
# row flips PENDING -> APPROVED the (zone_id, kind, subject) tuple is freed.
# A caller arriving within this window inherits the prior approval (for
# session/persist scopes) or, for ONCE scope, is queued normally as a fresh
# request — preserving operator intent.
_INHERIT_GRACE_SECONDS = 2.0
_INHERITABLE_SCOPES: frozenset[DecisionScope] = frozenset(
    {
        DecisionScope.SESSION,
        DecisionScope.PERSIST_SANDBOX,
        DecisionScope.PERSIST_BASELINE,
    }
)


@dataclass(frozen=True)
class WatchEvent:
    type: str  # "pending" | "decided"
    request_id: str
    zone_id: str
    decision: str | None


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
        self._watchers: list[tuple[str | None, asyncio.Queue[WatchEvent]]] = []
        self._sweeper = Sweeper(
            repository=repository,
            interval_seconds=config.sweeper_interval_seconds,
            on_expired=self._on_expired_ids,
        )

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
        await self._sweeper.start()

    async def stop(self) -> None:
        await self._sweeper.stop()
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

        # Option 2: session-scope cache short-circuit. Mirrors PolicyGate.check
        # so non-PolicyGate callers (gRPC, HTTP, internal) also benefit and so
        # callers arriving after the orphan-race window still get APPROVED if a
        # session_allow row was inserted by the SESSION-scope decide.
        if session_id is not None:
            allow = await self._repo.session_allow_exists(
                session_id=session_id,
                zone_id=zone_id,
                kind=kind,
                subject=subject,
            )
            if allow:
                return Decision.APPROVED

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
        newly_inserted = req.id == request_id
        if newly_inserted:
            # Option 1: late-insert orphan race. If a sibling row for the same
            # coalesce key was decided in the last `_INHERIT_GRACE_SECONDS`,
            # propagate the decision instead of stranding this caller until
            # timeout. Operator intent is honored per scope: ONCE => fresh
            # prompt, SESSION/PERSIST => inherit. See module-level constant
            # for the rationale.
            inherited = await self._maybe_inherit_recent_decision(
                request_id=req.id,
                zone_id=zone_id,
                kind=kind,
                subject=subject,
                session_id=session_id,
                now=now,
            )
            if inherited is not None:
                return inherited

            try:
                await self._notify.notify(
                    CHANNEL_NEW,
                    json.dumps({"request_id": req.id, "zone_id": zone_id}),
                )
            except Exception:
                logger.warning("notify(approvals_new) failed; queue still durable", exc_info=True)

        fut = self._dispatcher.register(req.id, session_id=session_id)

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

    async def _maybe_inherit_recent_decision(
        self,
        *,
        request_id: str,
        zone_id: str,
        kind: ApprovalKind,
        subject: str,
        session_id: str | None,
        now: datetime,
    ) -> Decision | None:
        """Inherit a recent terminal decision for a late-inserted row.

        Returns the propagated Decision, or None to fall through to the normal
        wait path. Only inherits APPROVED with non-ONCE scope; ONCE scope is
        skipped on purpose so the operator gets a fresh prompt for each call.
        REJECTED/EXPIRED are also not inherited — operator intent for a
        previous attempt should not silently deny a new caller (the late
        caller may be answering a separate retry). See Issue #3790 follow-up.
        """
        since = now - timedelta(seconds=_INHERIT_GRACE_SECONDS)
        recent = await self._repo.get_recent_decision(
            zone_id=zone_id,
            kind=kind,
            subject=subject,
            since=since,
            exclude_request_id=request_id,
        )
        if recent is None:
            return None
        if recent.status is not ApprovalRequestStatus.APPROVED:
            return None
        scope = recent.decision_scope
        if scope is None or scope not in _INHERITABLE_SCOPES:
            return None

        # Flip our late-inserted row to APPROVED with the inherited scope.
        # If the transition fails (already decided by a concurrent operator,
        # which is possible with Postgres NOTIFY arrival), fall through.
        decided_by = recent.decided_by or "system"
        updated = await self._repo.transition(
            request_id=request_id,
            new_status=ApprovalRequestStatus.APPROVED,
            decided_by=decided_by,
            scope=scope,
            reason=f"inherited_from_{recent.id}",
            source=DecisionSource.SYSTEM_INHERITED,
            now=now,
        )
        if updated is None:
            return None

        # For SESSION scope, also persist a session_allow row so subsequent
        # callers (with the same session_id) short-circuit at the top of
        # request_and_wait without round-tripping through this branch.
        if scope is DecisionScope.SESSION and session_id is not None:
            await self._repo.insert_session_allow(
                session_id=session_id,
                zone_id=zone_id,
                kind=kind,
                subject=subject,
                decided_by=decided_by,
                decided_at=now,
                request_id=request_id,
            )

        # Notify other workers so any in-process futures registered against
        # our id are resolved promptly. Local dispatcher is idempotent — no
        # local futures yet (we inherit before register) but be safe.
        try:
            await self._notify.notify(
                CHANNEL_DECIDED,
                json.dumps({"request_id": request_id, "decision": Decision.APPROVED.value}),
            )
        except Exception:
            logger.warning(
                "notify(approvals_decided) failed during inherit; row durable",
                exc_info=True,
            )
        self._dispatcher.resolve(request_id, Decision.APPROVED)
        return Decision.APPROVED

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

        if scope is DecisionScope.SESSION and decision is Decision.APPROVED:
            # Fan out a session_allow row for *every* coalesced waiter's
            # session_id, not just ``updated.session_id`` (the winning
            # insert's). When N callers coalesce on (zone, kind, subject)
            # with N different sessions, a SESSION-scope approval should
            # short-circuit *all* of them on the next same-session call —
            # otherwise the losers fall back to a fresh pending row after
            # the inherit window. (Issue #3790 follow-up.)
            session_ids: set[str] = set()
            if updated.session_id:
                session_ids.add(updated.session_id)
            for sid in self._dispatcher.session_ids_for(request_id):
                session_ids.add(sid)
            for sid in session_ids:
                await self._repo.insert_session_allow(
                    session_id=sid,
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
        # NB: resolve() drops the dispatcher entry — must run after the
        # session_ids_for(...) fan-out above, not before.
        self._dispatcher.resolve(request_id, decision)
        return updated

    async def list_pending(self, zone_id: str | None) -> list[ApprovalRequest]:
        return await self._repo.list_pending(zone_id)

    async def get(self, request_id: str) -> ApprovalRequest | None:
        return await self._repo.get(request_id)

    async def cancel(self, future: asyncio.Future[Decision]) -> None:
        self._dispatcher.cancel(future)

    async def reconcile_in_flight(self) -> None:
        """Re-resolve futures for any in-flight request that already terminated.

        Call after a LISTEN reconnect to recover from missed notifications.
        """
        for rid in self._dispatcher.in_flight_request_ids():
            row = await self._repo.get(rid)
            if row is None:
                continue
            if row.status is ApprovalRequestStatus.APPROVED:
                self._dispatcher.resolve(rid, Decision.APPROVED)
            elif row.status in (
                ApprovalRequestStatus.REJECTED,
                ApprovalRequestStatus.EXPIRED,
            ):
                self._dispatcher.resolve(rid, Decision.DENIED)

    async def watch(self, zone_id: str | None) -> AsyncIterator[WatchEvent]:
        q: asyncio.Queue[WatchEvent] = asyncio.Queue(maxsize=self._cfg.watch_buffer_size)
        entry = (zone_id, q)
        self._watchers.append(entry)
        try:
            while True:
                ev = await q.get()
                yield ev
        finally:
            with contextlib.suppress(ValueError):
                self._watchers.remove(entry)

    # ------------------------------------------------------------------
    # Sweeper callback
    # ------------------------------------------------------------------

    def _on_expired_ids(self, ids: list[str]) -> None:
        for rid in ids:
            self._dispatcher.resolve(rid, Decision.DENIED)
            self._broadcast(
                WatchEvent(type="decided", request_id=rid, zone_id="", decision="expired")
            )

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

        # F2 (Issue #3790): cross-worker SESSION fan-out. Before resolving
        # the local dispatcher (which drops the session_ids of any
        # in-process waiters), snapshot every waiter's session_id so we
        # can write idempotent ``session_allow`` rows on this worker too.
        # The deciding worker already wrote rows for ITS own local
        # waiters (in ``decide``); without this branch, any caller who
        # registered on a DIFFERENT worker would be unblocked by the
        # NOTIFY but never have their session_allow row persisted —
        # subsequent same-session calls would hit a fresh PENDING row
        # instead of short-circuiting.
        local_session_ids = self._dispatcher.session_ids_for(rid)

        # Resolve local futures first (latency-sensitive — same shape as
        # before; the session_allow inserts are best-effort followups).
        self._dispatcher.resolve(rid, decision)

        # zone is not on the payload; look it up so we can broadcast
        # the WatchEvent with the right zone AND so the SESSION fan-out
        # below has the kind/subject/zone tuple.
        row = await self._repo.get(rid)
        zone = row.zone_id if row else ""
        self._broadcast(
            WatchEvent(type="decided", request_id=rid, zone_id=zone, decision=decision.value)
        )

        # F2: only fan out for APPROVED + SESSION scope. The repo's
        # ``insert_session_allow`` is keyed on the partial unique index
        # ``uq_approval_session_allow`` so duplicates from the
        # deciding-worker insert (same row) are silently skipped.
        if (
            decision is Decision.APPROVED
            and row is not None
            and row.decision_scope is DecisionScope.SESSION
            and local_session_ids
        ):
            decided_at = row.decided_at or datetime.now(UTC)
            decided_by = row.decided_by or "system"
            for sid in local_session_ids:
                try:
                    await self._repo.insert_session_allow(
                        session_id=sid,
                        zone_id=row.zone_id,
                        kind=row.kind,
                        subject=row.subject,
                        decided_by=decided_by,
                        decided_at=decided_at,
                        request_id=row.id,
                    )
                except Exception:
                    # Best-effort: a failure here only means the
                    # session_allow short-circuit won't fire on the
                    # *next* same-session call from this caller. The
                    # current waiter is already resolved.
                    logger.warning(
                        "approvals: cross-worker session_allow insert failed rid=%s sid=%s",
                        rid,
                        sid,
                        exc_info=True,
                    )

    async def _on_new_payload(self, payload: str) -> None:
        try:
            msg = json.loads(payload)
            rid = msg["request_id"]
            zone = msg["zone_id"]
        except Exception:
            return
        self._broadcast(WatchEvent(type="pending", request_id=rid, zone_id=zone, decision=None))

    def _broadcast(self, ev: WatchEvent) -> None:
        for zone, q in list(self._watchers):
            if zone is not None and zone != ev.zone_id:
                continue
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                # Slow watcher: drop and let it reconcile via list_pending.
                logger.warning("watch buffer overflow; dropping event for %s", ev.request_id)


def _row_to_decision(row: ApprovalRequest, *, timeout: float) -> Decision:
    if row.status is ApprovalRequestStatus.APPROVED:
        return Decision.APPROVED
    if row.status is ApprovalRequestStatus.REJECTED:
        raise ApprovalDenied(row.id, "rejected")
    if row.status is ApprovalRequestStatus.EXPIRED:
        raise ApprovalTimeout(row.id, timeout)
    raise RuntimeError(f"unexpected status {row.status}")
