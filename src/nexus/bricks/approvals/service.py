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

# F2 (Issue #3790): session_id prefixes that MUST NOT short-circuit the
# SESSION-scope cache. These are server-fabricated identifiers (no
# HTTP-session lifecycle binds them) — caching against them would turn
# SESSION-scoped operator approval into a durable persist, which is the
# operator's call to make via a real ReBAC tuple, not a side-effect of
# an approval queue grant.
#
# ``hub:`` — synthesized by the hub zone-access hook (see
# nexus.server.auth.zone_routes._zone_access_approved_via_gate). Stable
# across requests and independent of HTTP-session lifetime.
_FABRICATED_SESSION_ID_PREFIXES: tuple[str, ...] = ("hub:",)


def _is_fabricated_session_id(session_id: str | None) -> bool:
    """Return True if ``session_id`` is a server-fabricated identifier.

    Fabricated session_ids (see ``_FABRICATED_SESSION_ID_PREFIXES``) are
    not bound to any caller-controlled session lifetime; consulting the
    SESSION-scope cache against them would make a SESSION-scope approval
    effectively durable. This helper centralizes the rule so
    ``request_and_wait`` / ``PolicyGate.check`` / ``decide`` can all
    refuse the fast-path consistently.
    """
    if session_id is None:
        return False
    return any(session_id.startswith(p) for p in _FABRICATED_SESSION_ID_PREFIXES)


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
        # F3 (#3790): reconcile watchdog. Set in start(), cancelled in stop().
        # Periodically runs ``reconcile_in_flight`` so cross-worker decisions
        # converge even if NOTIFY delivery silently fails (asyncpg listener
        # disconnect, missed payload, etc.). See start() for the cadence.
        self._reconcile_task: asyncio.Task[None] | None = None

    @property
    def repository(self) -> ApprovalRepository:
        """Public accessor — used by PolicyGate (Task 13) for session_allow."""
        return self._repo

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        # F3 (Issue #3790): pass reconcile_in_flight as the bridge's
        # ``on_reconnect`` hook so any rows decided while the LISTEN
        # connection was unavailable get flushed back to local waiters
        # the first time the listener attaches. The hook fires once on
        # initial start today; future auto-reconnect plumbing in
        # NotifyBridge will re-fire it after each successful re-attach
        # without further changes here.
        await self._notify.start(
            {
                CHANNEL_DECIDED: self._on_decided_payload,
                CHANNEL_NEW: self._on_new_payload,
            },
            on_reconnect=self.reconcile_in_flight,
        )
        await self._sweeper.start()
        # F3 (#3790): start the reconcile watchdog if configured. The
        # watchdog runs at ``reconcile_interval_seconds`` cadence and
        # makes cross-worker decisions converge even when NOTIFY drops a
        # message (asyncpg has no auto-reconnect today, so a listener
        # blip can strand local waiters until timeout).
        if self._cfg.reconcile_interval_seconds > 0:
            self._reconcile_task = asyncio.create_task(self._reconcile_loop())

    async def stop(self) -> None:
        # Stop the reconcile watchdog before tearing down NotifyBridge so
        # an in-flight reconcile doesn't race against repository teardown.
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            with contextlib.suppress(BaseException):
                await self._reconcile_task
            self._reconcile_task = None
        await self._sweeper.stop()
        await self._notify.stop()

    async def _reconcile_loop(self) -> None:
        """Periodic watchdog — sweeps in-flight futures against the DB.

        F3 (#3790): NotifyBridge holds one asyncpg connection for LISTEN
        and has no auto-reconnect path today. If that connection drops,
        rows decided on a remote worker won't fire ``_on_decided_payload``
        locally and the in-process futures wait until ``request_and_wait``
        times them out. The watchdog re-fetches every in-flight row at a
        bounded cadence so any terminal status flushes the future
        regardless of NOTIFY health. Cancel-aware: the task exits cleanly
        on ``stop()``.
        """
        interval = self._cfg.reconcile_interval_seconds
        while True:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            try:
                await self.reconcile_in_flight()
            except asyncio.CancelledError:
                return
            except Exception:
                # Reconcile is best-effort — a single failure must not
                # kill the watchdog loop. Log and try again next tick.
                logger.warning(
                    "approvals.reconcile_in_flight raised in watchdog; will retry",
                    exc_info=True,
                )

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
        #
        # F2 (#3790): refuse the fast-path for server-fabricated session_ids
        # (e.g. the hub zone-access hook's stable ``hub:user:zone:...`` shape).
        # Those identifiers have no HTTP-session lifetime, so honoring them
        # here would turn a SESSION-scope approval into a durable persist
        # without the operator ever asking for it. Falling through means
        # the next call goes back through the queue.
        if session_id is not None and not _is_fabricated_session_id(session_id):
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

        if req is None:
            # Round-4 (#3790): the ON CONFLICT path returned None because
            # the conflicting pending row was decided between the insert
            # and the follow-up SELECT (operator decision arrived in that
            # tiny window). Treat this as a very-recent decision: check
            # the inherit path with a fresh timestamp and, for SESSION/
            # PERSIST scopes, return APPROVED if a session_allow row was
            # just written. Failing that, raise GatewayClosed so the
            # caller retries — a re-try will either insert a fresh row or
            # hit the session_allow fast-path above.
            inherited = await self._maybe_inherit_recent_decision(
                request_id=request_id,
                zone_id=zone_id,
                kind=kind,
                subject=subject,
                session_id=session_id,
                now=datetime.now(UTC),
            )
            if inherited is not None:
                return inherited
            raise GatewayClosed("coalesced pending row vanished (concurrent decide); retry")

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
            # Round-4 (#3790): re-read the row before writing EXPIRED.
            # The wait_for timeout is a LOCAL caller's configured limit,
            # not the row's durable ``expires_at``. For coalesced
            # requests one waiter with a shorter timeout must NOT expire
            # the shared DB row while other waiters and the operator
            # still have time under ``row.expires_at``. Similarly, if a
            # remote worker already approved the row but NOTIFY was
            # dropped, the local waiter would have timed out before the
            # watchdog ran — overwriting APPROVED with EXPIRED here would
            # turn a valid grant into a denial.
            #
            # Only attempt the EXPIRED transition when the row's own
            # ``expires_at`` is in the past (or absent). If the row is
            # already terminal, honor that decision instead of raising
            # ApprovalTimeout.
            try:
                _latest = await self._repo.get(req.id)
            except Exception:
                _latest = None

            if _latest is not None and _latest.status is not ApprovalRequestStatus.PENDING:
                # Row already terminal (approved, rejected, or expired by
                # sweeper / another waiter). Honor the DB decision.
                self._dispatcher.cancel(fut)
                try:
                    return _row_to_decision(_latest, timeout=timeout)
                except ApprovalTimeout:
                    pass  # truly expired — fall through to raise below

            _now_ts = datetime.now(UTC)
            _row_expired = (
                _latest is None or _latest.expires_at is None or _latest.expires_at <= _now_ts
            )
            if _row_expired:
                # Safe to expire: the stored expires_at is past (or
                # missing), so no other waiter has remaining time under
                # the row's own expiry. Best-effort: a failed transition
                # means the sweeper handles the row.
                _expired_ok = False
                try:
                    await self._repo.transition(
                        request_id=req.id,
                        new_status=ApprovalRequestStatus.EXPIRED,
                        decided_by="system",
                        scope=DecisionScope.ONCE,
                        reason="auto_deny_after_timeout",
                        source=DecisionSource.SYSTEM_TIMEOUT,
                        now=_now_ts,
                    )
                    _expired_ok = True
                except Exception:
                    logger.warning(
                        "approvals.request_and_wait: best-effort EXPIRED transition "
                        "failed rid=%s (sweeper will reconcile)",
                        req.id,
                        exc_info=True,
                    )

                if _expired_ok:
                    # Round-4 (#3790): wake coalesced waiters so they do
                    # not block until the watchdog or their own timeout.
                    # Mirrors ``_on_expired_ids`` but scoped to this row.
                    self._dispatcher.resolve(req.id, Decision.DENIED)
                    zone = (
                        _latest.zone_id
                        if _latest is not None
                        else req.zone_id
                        if hasattr(req, "zone_id")
                        else ""
                    )
                    self._broadcast(
                        WatchEvent(
                            type="decided",
                            request_id=req.id,
                            zone_id=zone,
                            decision="expired",
                        )
                    )
                    try:
                        await self._notify.notify(
                            CHANNEL_DECIDED,
                            json.dumps({"request_id": req.id, "decision": Decision.DENIED.value}),
                        )
                    except Exception:
                        logger.warning(
                            "approvals: notify(decided) after local EXPIRED failed rid=%s",
                            req.id,
                            exc_info=True,
                        )
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
        # F2 (#3790): skip when the session_id was fabricated server-side
        # (e.g. by the hub zone-access hook) — durable caching against an
        # un-bound identifier is the operator's call via a real ReBAC
        # tuple, not an inheritance side-effect.
        if (
            scope is DecisionScope.SESSION
            and session_id is not None
            and not _is_fabricated_session_id(session_id)
        ):
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
            # F1 (#3790): transition() also returns None when the row's
            # expires_at is already past (an operator approving a stale
            # request). The caller (gRPC Decide) maps ValueError to
            # FAILED_PRECONDITION which is the right status for both
            # "already decided" and "expired"; keep the message generic
            # so we don't leak the row's expiry state.
            raise ValueError(f"request {request_id} not pending or already expired")

        # F3 (Issue #3790): once ``transition`` has committed, the
        # row is non-pending and a retry can't re-decide it. We MUST
        # resolve local dispatcher futures and emit NOTIFY even if any
        # subsequent best-effort step (session_allow inserts, NOTIFY
        # publish) fails — otherwise local waiters strand until timeout
        # and cross-worker waiters never get unblocked.
        #
        # Order:
        #   1. snapshot session_ids (resolve drops the dispatcher entry)
        #   2. resolve local futures — IMMEDIATE, no try/except
        #   3. session_allow inserts (best-effort, log+swallow)
        #   4. NOTIFY (best-effort, log+swallow)
        #
        # Reconciliation (``reconcile_in_flight``) still recovers any
        # cross-worker waiter on a NOTIFY drop — see service.start().

        # Step 1: snapshot session_ids BEFORE resolve drops them.
        # F2 (#3790): server-fabricated session_ids are excluded so a
        # SESSION-scope approval against them does not fan out durable
        # session_allow rows (the next caller must go back through the
        # queue — see ``_is_fabricated_session_id``).
        session_ids: set[str] = set()
        if scope is DecisionScope.SESSION and decision is Decision.APPROVED:
            if updated.session_id and not _is_fabricated_session_id(updated.session_id):
                session_ids.add(updated.session_id)
            for sid in self._dispatcher.session_ids_for(request_id):
                if not _is_fabricated_session_id(sid):
                    session_ids.add(sid)

        # Step 2: resolve in-process futures. NEVER let a downstream
        # error rob local callers of the decision they were waiting on.
        self._dispatcher.resolve(request_id, decision)

        # Step 3: best-effort session_allow fan-out for SESSION scope.
        # Each insert is wrapped individually so a single failure can't
        # block the others or the NOTIFY publish.
        for sid in session_ids:
            try:
                await self._repo.insert_session_allow(
                    session_id=sid,
                    zone_id=updated.zone_id,
                    kind=updated.kind,
                    subject=updated.subject,
                    decided_by=decided_by,
                    decided_at=now,
                    request_id=updated.id,
                )
            except Exception:
                logger.warning(
                    "approvals.decide: session_allow insert failed sid=%s rid=%s "
                    "(row already APPROVED; reconciliation can address later)",
                    sid,
                    request_id,
                    exc_info=True,
                )

        # Step 4: best-effort NOTIFY for cross-worker fan-out.
        # Cross-worker waiters that miss this NOTIFY get unblocked via
        # ``reconcile_in_flight`` after the listener reconnects.
        try:
            await self._notify.notify(
                CHANNEL_DECIDED,
                json.dumps({"request_id": request_id, "decision": decision.value}),
            )
        except Exception:
            logger.warning(
                "approvals.decide: NOTIFY publish failed rid=%s "
                "(row already APPROVED; cross-worker waiters will reconcile)",
                request_id,
                exc_info=True,
            )

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

        F3 (#3790): mirrors ``_on_decided_payload``'s SESSION fan-out so
        callers waiting on a SESSION-scope APPROVED row that arrived via
        a missed NOTIFY still get their ``session_allow`` rows persisted
        — without this, a subsequent same-session call would reopen a
        fresh PENDING instead of short-circuiting via the cache.
        """
        for rid in self._dispatcher.in_flight_request_ids():
            row = await self._repo.get(rid)
            if row is None:
                continue
            # F3 (#3790): snapshot session_ids BEFORE resolve drops the
            # dispatcher entry. We reuse the same fan-out shape as
            # ``_on_decided_payload`` (best-effort, fabricated-id
            # filter, idempotent inserts via the partial unique index).
            local_session_ids = self._dispatcher.session_ids_for(rid)
            if row.status is ApprovalRequestStatus.APPROVED:
                self._dispatcher.resolve(rid, Decision.APPROVED)
                if row.decision_scope is DecisionScope.SESSION and local_session_ids:
                    decided_at = row.decided_at or datetime.now(UTC)
                    decided_by = row.decided_by or "system"
                    for sid in local_session_ids:
                        if _is_fabricated_session_id(sid):
                            continue
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
                            # Best-effort: the future is already
                            # resolved; a failed insert only means the
                            # next same-session caller goes back through
                            # the queue.
                            logger.warning(
                                "approvals.reconcile: session_allow insert failed rid=%s sid=%s",
                                rid,
                                sid,
                                exc_info=True,
                            )
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
        # Issue #3790 F2 follow-up: filter fabricated session_ids — see
        # ``_is_fabricated_session_id`` for rationale.
        if (
            decision is Decision.APPROVED
            and row is not None
            and row.decision_scope is DecisionScope.SESSION
            and local_session_ids
        ):
            decided_at = row.decided_at or datetime.now(UTC)
            decided_by = row.decided_by or "system"
            for sid in local_session_ids:
                if _is_fabricated_session_id(sid):
                    continue
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
