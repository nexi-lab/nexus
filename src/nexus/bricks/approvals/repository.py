"""Repository facade over the three approval tables.

All public methods are async; they hide SQL from the service layer.
The transition() method enforces single-decision atomicity via
UPDATE ... WHERE status='pending' RETURNING ...; callers receive None when
the row was already decided/expired.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nexus.bricks.approvals.db_models import (
    ApprovalDecisionModel,
    ApprovalRequestModel,
    ApprovalSessionAllowModel,
)
from nexus.bricks.approvals.models import (
    ApprovalKind,
    ApprovalRequest,
    ApprovalRequestStatus,
    DecisionScope,
    DecisionSource,
)

SessionFactory = async_sessionmaker[AsyncSession]


# ORM stores enum values as plain strings; domain objects use enum types.
# Public methods accept/return enums — coercion to `.value` happens at the
# SQL boundary inside this module.
def _to_domain(row: ApprovalRequestModel) -> ApprovalRequest:
    return ApprovalRequest(
        id=row.id,
        zone_id=row.zone_id,
        kind=ApprovalKind(row.kind),
        subject=row.subject,
        agent_id=row.agent_id,
        token_id=row.token_id,
        session_id=row.session_id,
        reason=row.reason,
        metadata=row.metadata_ or {},
        status=ApprovalRequestStatus(row.status),
        created_at=row.created_at,
        decided_at=row.decided_at,
        decided_by=row.decided_by,
        decision_scope=DecisionScope(row.decision_scope) if row.decision_scope else None,
        expires_at=row.expires_at,
    )


class ApprovalRepository:
    """Async repository for approval queue persistence."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def insert_or_fetch_pending(
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
        now: datetime,
        expires_at: datetime,
    ) -> ApprovalRequest | None:
        """Insert pending row OR return the existing one for the coalesce key.

        Returns None in the rare race where the conflicting row was decided
        between the ON CONFLICT and the follow-up SELECT (Round-4 #3790).
        Callers must handle None by checking for a recent terminal decision.

        Race-safe: relies on the partial unique index
        approval_requests_pending_coalesce.
        """
        async with self._session_factory() as session:
            stmt = (
                pg_insert(ApprovalRequestModel)
                .values(
                    id=request_id,
                    zone_id=zone_id,
                    kind=kind.value,
                    subject=subject,
                    agent_id=agent_id,
                    token_id=token_id,
                    session_id=session_id,
                    reason=reason,
                    metadata_=metadata,
                    status=ApprovalRequestStatus.PENDING.value,
                    created_at=now,
                    expires_at=expires_at,
                )
                .on_conflict_do_nothing(
                    index_elements=["zone_id", "kind", "subject"],
                    index_where=text("status = 'pending'"),
                )
                .returning(ApprovalRequestModel)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is not None:
                await session.commit()
                return _to_domain(row)

            # Conflict: fetch the existing pending row. Use
            # scalar_one_or_none() — an operator decision can commit
            # between the ON CONFLICT and this SELECT, removing the row
            # from the partial index (Round-4 #3790). If that happens
            # return None so request_and_wait can fall through to the
            # recent-decision inherit path rather than raising
            # GatewayClosed.
            existing = (
                await session.execute(
                    select(ApprovalRequestModel).where(
                        ApprovalRequestModel.zone_id == zone_id,
                        ApprovalRequestModel.kind == kind.value,
                        ApprovalRequestModel.subject == subject,
                        ApprovalRequestModel.status == ApprovalRequestStatus.PENDING.value,
                    )
                )
            ).scalar_one_or_none()
            await session.commit()
            if existing is None:
                return None
            return _to_domain(existing)

    async def get(self, request_id: str) -> ApprovalRequest | None:
        async with self._session_factory() as session:
            row = await session.get(ApprovalRequestModel, request_id)
            return _to_domain(row) if row else None

    async def list_pending(self, zone_id: str | None) -> list[ApprovalRequest]:
        async with self._session_factory() as session:
            stmt = select(ApprovalRequestModel).where(
                ApprovalRequestModel.status == ApprovalRequestStatus.PENDING.value
            )
            if zone_id is not None:
                stmt = stmt.where(ApprovalRequestModel.zone_id == zone_id)
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_domain(r) for r in rows]

    async def transition(
        self,
        *,
        request_id: str,
        new_status: ApprovalRequestStatus,
        decided_by: str,
        scope: DecisionScope,
        reason: str | None,
        source: DecisionSource,
        now: datetime,
    ) -> ApprovalRequest | None:
        """Atomic UPDATE pending → new_status. Returns None if not pending.

        F1 (#3790): when transitioning to APPROVED/REJECTED, the row's
        ``expires_at`` MUST also be in the future. Without this guard
        ``request_and_wait`` callers who time out locally leave a stale
        pending row in the DB until the periodic sweeper runs — and an
        operator decision arriving in that window would write a
        SESSION-scope ``session_allow`` row for an already-auto-denied
        request, granting future calls the original gate already rejected.
        EXPIRED transitions are exempt: the sweeper (and the local
        timeout path in service.request_and_wait) drive rows past their
        expiry into EXPIRED, by definition operating on rows where
        ``expires_at <= now``.
        """
        async with self._session_factory() as session:
            stmt = update(ApprovalRequestModel).where(
                ApprovalRequestModel.id == request_id,
                ApprovalRequestModel.status == ApprovalRequestStatus.PENDING.value,
            )
            # F1 (#3790): refuse APPROVED/REJECTED on rows whose
            # ``expires_at`` is already past. EXPIRED transitions
            # bypass this guard — those are exactly the rows the
            # sweeper / local-timeout path needs to flip.
            if new_status is not ApprovalRequestStatus.EXPIRED:
                stmt = stmt.where(ApprovalRequestModel.expires_at > now)
            stmt = stmt.values(
                status=new_status.value,
                decided_at=now,
                decided_by=decided_by,
                decision_scope=scope.value,
            ).returning(ApprovalRequestModel)
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                await session.commit()
                return None

            await session.execute(
                insert(ApprovalDecisionModel).values(
                    request_id=request_id,
                    decided_at=now,
                    decided_by=decided_by,
                    decision=new_status.value,
                    scope=scope.value,
                    reason=reason,
                    source=source.value,
                )
            )
            await session.commit()
            return _to_domain(row)

    async def insert_session_allow(
        self,
        *,
        session_id: str,
        zone_id: str,
        kind: ApprovalKind,
        subject: str,
        decided_by: str,
        decided_at: datetime,
        request_id: str | None,
    ) -> None:
        async with self._session_factory() as session:
            stmt = (
                pg_insert(ApprovalSessionAllowModel)
                .values(
                    session_id=session_id,
                    zone_id=zone_id,
                    kind=kind.value,
                    subject=subject,
                    decided_by=decided_by,
                    decided_at=decided_at,
                    request_id=request_id,
                )
                .on_conflict_do_nothing(constraint="uq_approval_session_allow")
            )
            await session.execute(stmt)
            await session.commit()

    async def session_allow_exists(
        self,
        *,
        session_id: str,
        zone_id: str,
        kind: ApprovalKind,
        subject: str,
    ) -> bool:
        """Check if a session-scoped allow row exists for this 4-tuple."""
        async with self._session_factory() as session:
            stmt = (
                select(ApprovalSessionAllowModel.id)
                .where(
                    ApprovalSessionAllowModel.session_id == session_id,
                    ApprovalSessionAllowModel.zone_id == zone_id,
                    ApprovalSessionAllowModel.kind == kind.value,
                    ApprovalSessionAllowModel.subject == subject,
                )
                .limit(1)
            )
            return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def get_recent_decision(
        self,
        *,
        zone_id: str,
        kind: ApprovalKind,
        subject: str,
        since: datetime,
        exclude_request_id: str | None = None,
    ) -> ApprovalRequest | None:
        """Return the most recent terminal row for the coalesce key after `since`.

        Used to plug the "late insert orphan" race: once a pending row is
        flipped to APPROVED/REJECTED/EXPIRED, the partial unique index
        ``approval_requests_pending_coalesce`` (``WHERE status='pending'``)
        frees the (zone_id, kind, subject) tuple for re-insertion. A caller
        arriving milliseconds after the decide may insert a fresh pending row
        that no operator is watching. The service consults this method to
        propagate the recent decision (when scope semantics permit) instead
        of stranding the late caller until timeout.
        """
        async with self._session_factory() as session:
            stmt = (
                select(ApprovalRequestModel)
                .where(
                    ApprovalRequestModel.zone_id == zone_id,
                    ApprovalRequestModel.kind == kind.value,
                    ApprovalRequestModel.subject == subject,
                    ApprovalRequestModel.status != ApprovalRequestStatus.PENDING.value,
                    ApprovalRequestModel.decided_at >= since,
                )
                .order_by(ApprovalRequestModel.decided_at.desc())
                .limit(1)
            )
            if exclude_request_id is not None:
                stmt = stmt.where(ApprovalRequestModel.id != exclude_request_id)
            row = (await session.execute(stmt)).scalar_one_or_none()
            return _to_domain(row) if row else None

    async def sweep_expired(self, now: datetime) -> list[str]:
        """Mark all pending past-expires rows as expired and return their ids."""
        async with self._session_factory() as session:
            stmt = (
                update(ApprovalRequestModel)
                .where(
                    ApprovalRequestModel.status == ApprovalRequestStatus.PENDING.value,
                    ApprovalRequestModel.expires_at < now,
                )
                .values(
                    status=ApprovalRequestStatus.EXPIRED.value,
                    decided_at=now,
                    decided_by="system",
                    decision_scope=DecisionScope.ONCE.value,
                )
                .returning(ApprovalRequestModel.id)
            )
            ids = list((await session.execute(stmt)).scalars().all())
            for rid in ids:
                await session.execute(
                    insert(ApprovalDecisionModel).values(
                        request_id=rid,
                        decided_at=now,
                        decided_by="system",
                        decision=ApprovalRequestStatus.EXPIRED.value,
                        scope=DecisionScope.ONCE.value,
                        reason="auto_deny_after_timeout",
                        source=DecisionSource.SYSTEM_TIMEOUT.value,
                    )
                )
            await session.commit()
            return ids
