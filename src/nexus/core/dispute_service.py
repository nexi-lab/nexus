"""Dispute resolution service (Issue #1356, Decision #4A).

Tier 1 auto-mediation with forward-compatible data model. State machine:

    filed → auto_mediating → resolved | dismissed
    filed → dismissed

All other transitions are invalid and raise InvalidTransitionError.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar

from sqlalchemy import select

from nexus.core.reputation_records import DisputeRecord
from nexus.storage.models._base import _generate_uuid
from nexus.storage.models.dispute import DisputeModel
from nexus.storage.session_mixin import SessionMixin

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

# Appeal deadline: 7 days after resolution
_APPEAL_WINDOW_DAYS = 7


class DisputeService(SessionMixin):
    """Dispute lifecycle management with state machine enforcement.

    Args:
        session_factory: SQLAlchemy sessionmaker for database access.
    """

    VALID_TRANSITIONS: ClassVar[dict[str, set[str]]] = {
        "filed": {"auto_mediating", "dismissed"},
        "auto_mediating": {"resolved", "dismissed"},
        "resolved": set(),
        "dismissed": set(),
    }

    def __init__(
        self,
        session_factory: sessionmaker[Session],
    ) -> None:
        self._session_factory = session_factory

    def file_dispute(
        self,
        exchange_id: str,
        complainant_agent_id: str,
        respondent_agent_id: str,
        zone_id: str,
        reason: str,
        evidence_hash: str | None = None,
    ) -> DisputeRecord:
        """File a new dispute for an exchange.

        Args:
            exchange_id: The disputed exchange.
            complainant_agent_id: Agent filing the dispute.
            respondent_agent_id: Agent being complained about.
            zone_id: Zone scope.
            reason: Reason for the dispute.
            evidence_hash: Optional SHA-256 hash of evidence.

        Returns:
            DisputeRecord snapshot.

        Raises:
            DuplicateDisputeError: A dispute already exists for this exchange.
            ValueError: Complainant and respondent are the same agent.
        """
        if complainant_agent_id == respondent_agent_id:
            msg = "Cannot file dispute against yourself"
            raise ValueError(msg)

        with self._get_session() as session:
            # Check for existing dispute
            existing = session.execute(
                select(DisputeModel).where(DisputeModel.exchange_id == exchange_id)
            ).scalar_one_or_none()

            if existing is not None:
                raise DuplicateDisputeError(f"Dispute already exists for exchange {exchange_id}")

            model = DisputeModel(
                id=_generate_uuid(),
                exchange_id=exchange_id,
                zone_id=zone_id,
                complainant_agent_id=complainant_agent_id,
                respondent_agent_id=respondent_agent_id,
                status="filed",
                tier=1,
                reason=reason,
                resolution_evidence_hash=evidence_hash,
            )
            session.add(model)
            session.flush()

            record = self._model_to_record(model)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[DISPUTE] Filed: %s vs %s (exchange=%s)",
                complainant_agent_id,
                respondent_agent_id,
                exchange_id,
            )

        return record

    def auto_mediate(self, dispute_id: str) -> DisputeRecord:
        """Transition dispute to auto_mediating state.

        Args:
            dispute_id: Dispute to mediate.

        Returns:
            Updated DisputeRecord.

        Raises:
            DisputeNotFoundError: Dispute does not exist.
            InvalidTransitionError: Current state does not allow this transition.
        """
        with self._get_session() as session:
            model = self._transition(session, dispute_id, "auto_mediating")
            return self._model_to_record(model)

    def resolve(
        self,
        dispute_id: str,
        resolution: str,
        evidence_hash: str | None = None,
    ) -> DisputeRecord:
        """Resolve a dispute.

        Args:
            dispute_id: Dispute to resolve.
            resolution: Resolution description.
            evidence_hash: Optional SHA-256 hash of resolution evidence.

        Returns:
            Updated DisputeRecord.

        Raises:
            DisputeNotFoundError: Dispute does not exist.
            InvalidTransitionError: Current state does not allow this transition.
        """
        with self._get_session() as session:
            model = self._transition(session, dispute_id, "resolved")
            model.resolution = resolution
            model.resolution_evidence_hash = evidence_hash
            model.resolved_at = datetime.now(UTC)
            model.appeal_deadline = datetime.now(UTC) + timedelta(days=_APPEAL_WINDOW_DAYS)
            session.flush()
            return self._model_to_record(model)

    def dismiss(self, dispute_id: str, reason: str) -> DisputeRecord:
        """Dismiss a dispute.

        Args:
            dispute_id: Dispute to dismiss.
            reason: Reason for dismissal.

        Returns:
            Updated DisputeRecord.

        Raises:
            DisputeNotFoundError: Dispute does not exist.
            InvalidTransitionError: Current state does not allow this transition.
        """
        with self._get_session() as session:
            model = self._transition(session, dispute_id, "dismissed")
            model.resolution = reason
            model.resolved_at = datetime.now(UTC)
            session.flush()
            return self._model_to_record(model)

    def get_dispute(self, dispute_id: str) -> DisputeRecord | None:
        """Get a dispute by ID.

        Returns:
            DisputeRecord or None if not found.
        """
        with self._get_session() as session:
            model = session.execute(
                select(DisputeModel).where(DisputeModel.id == dispute_id)
            ).scalar_one_or_none()

            if model is None:
                return None

            return self._model_to_record(model)

    def list_disputes(
        self,
        exchange_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
        zone_id: str | None = None,
    ) -> list[DisputeRecord]:
        """List disputes with optional filters.

        Args:
            exchange_id: Filter by exchange ID.
            agent_id: Filter by agent (as complainant or respondent).
            status: Filter by status.
            zone_id: Filter by zone.

        Returns:
            List of DisputeRecord.
        """
        with self._get_session() as session:
            stmt = select(DisputeModel)

            if exchange_id is not None:
                stmt = stmt.where(DisputeModel.exchange_id == exchange_id)
            if agent_id is not None:
                stmt = stmt.where(
                    (DisputeModel.complainant_agent_id == agent_id)
                    | (DisputeModel.respondent_agent_id == agent_id)
                )
            if status is not None:
                stmt = stmt.where(DisputeModel.status == status)
            if zone_id is not None:
                stmt = stmt.where(DisputeModel.zone_id == zone_id)

            stmt = stmt.order_by(DisputeModel.filed_at.desc())

            models = list(session.execute(stmt).scalars().all())
            return [self._model_to_record(m) for m in models]

    def _transition(
        self,
        session: Session,
        dispute_id: str,
        new_status: str,
    ) -> DisputeModel:
        """Validate and apply a state transition.

        Raises:
            DisputeNotFoundError: Dispute does not exist.
            InvalidTransitionError: Transition not allowed.
        """
        model = session.execute(
            select(DisputeModel).where(DisputeModel.id == dispute_id)
        ).scalar_one_or_none()

        if model is None:
            raise DisputeNotFoundError(f"Dispute {dispute_id} not found")

        valid_next = self.VALID_TRANSITIONS.get(model.status, set())
        if new_status not in valid_next:
            raise InvalidTransitionError(
                f"Cannot transition from {model.status!r} to {new_status!r}"
            )

        model.status = new_status
        session.flush()
        return model

    @staticmethod
    def _model_to_record(model: DisputeModel) -> DisputeRecord:
        """Convert ORM model to frozen dataclass."""
        return DisputeRecord(
            id=model.id,
            exchange_id=model.exchange_id,
            zone_id=model.zone_id,
            complainant_agent_id=model.complainant_agent_id,
            respondent_agent_id=model.respondent_agent_id,
            status=model.status,
            tier=model.tier,
            reason=model.reason,
            resolution=model.resolution,
            resolution_evidence_hash=model.resolution_evidence_hash,
            escrow_amount=model.escrow_amount,
            escrow_released=model.escrow_released,
            filed_at=model.filed_at,
            resolved_at=model.resolved_at,
            appeal_deadline=model.appeal_deadline,
        )


class InvalidTransitionError(Exception):
    """Raised when a dispute state transition is not valid."""


class DisputeNotFoundError(Exception):
    """Raised when a dispute is not found."""


class DuplicateDisputeError(Exception):
    """Raised when a dispute already exists for an exchange."""
