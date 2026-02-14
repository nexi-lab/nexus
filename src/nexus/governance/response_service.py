"""Response actions service — throttle, suspend, appeal.

Issue #1359 Phase 4: Automatic throttling based on fraud scores,
agent suspension with appeal workflow, reputation integration.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from nexus.governance.approval.workflow import ApprovalWorkflow
from nexus.governance.models import (
    AnomalySeverity,
    ConstraintType,
    FraudScore,
    SuspensionRecord,
    ThrottleConfig,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from nexus.governance.anomaly_service import AnomalyService
    from nexus.governance.collusion_service import CollusionService
    from nexus.governance.governance_graph_service import GovernanceGraphService

logger = logging.getLogger(__name__)


class ResponseService:
    """Manages governance response actions.

    Responsibilities:
        - Auto-throttle agents based on fraud scores
        - Suspend agents (creates BLOCK constraints)
        - Handle suspension appeals (via ApprovalWorkflow)
        - Contribute fraud scores to reputation system
    """

    # Fraud score thresholds
    _THROTTLE_THRESHOLD: float = 0.5
    _BLOCK_THRESHOLD: float = 0.8

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        anomaly_service: AnomalyService | None = None,
        collusion_service: CollusionService | None = None,
        graph_service: GovernanceGraphService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._anomaly_service = anomaly_service
        self._collusion_service = collusion_service
        self._graph_service = graph_service
        self._appeal_workflow: ApprovalWorkflow[object] = ApprovalWorkflow(
            default_expiry_hours=168.0,
        )  # 7 days

    async def auto_throttle(
        self,
        agent_id: str,
        zone_id: str,
        fraud_score: FraudScore,
    ) -> ThrottleConfig | None:
        """Apply automatic throttling based on fraud score.

        Score > 0.5 → RATE_LIMIT constraint
        Score > 0.8 → BLOCK constraint
        """
        if fraud_score.score < self._THROTTLE_THRESHOLD:
            return None

        if fraud_score.score >= self._BLOCK_THRESHOLD and self._graph_service is not None:
            # Full block
            await self._graph_service.add_constraint(
                from_agent=agent_id,
                to_agent="*",  # Block all transactions
                zone_id=zone_id,
                constraint_type=ConstraintType.BLOCK,
                reason=f"Auto-blocked: fraud score {fraud_score.score:.2f}",
            )
            if logger.isEnabledFor(logging.WARNING):
                logger.warning(
                    "Auto-blocked agent=%s zone=%s (fraud_score=%.2f)",
                    agent_id,
                    zone_id,
                    fraud_score.score,
                )
            return None  # Blocked, not throttled

        # Rate limit
        throttle = ThrottleConfig(
            agent_id=agent_id,
            zone_id=zone_id,
            max_tx_per_hour=max(1, int(10 * (1 - fraud_score.score))),
            max_amount_per_day=max(1.0, 100.0 * (1 - fraud_score.score)),
            reason=f"Auto-throttled: fraud score {fraud_score.score:.2f}",
            applied_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )

        # Persist throttle
        await self._persist_throttle(throttle)

        # Add rate limit constraint
        if self._graph_service is not None:
            await self._graph_service.add_constraint(
                from_agent=agent_id,
                to_agent="*",
                zone_id=zone_id,
                constraint_type=ConstraintType.RATE_LIMIT,
                reason=throttle.reason,
            )

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Auto-throttled agent=%s zone=%s (fraud_score=%.2f, max_tx/hr=%d)",
                agent_id,
                zone_id,
                fraud_score.score,
                throttle.max_tx_per_hour,
            )

        return throttle

    async def suspend_agent(
        self,
        agent_id: str,
        zone_id: str,
        reason: str,
        duration_hours: float = 24.0,
        severity: AnomalySeverity = AnomalySeverity.HIGH,
    ) -> SuspensionRecord:
        """Suspend an agent for a specified duration.

        Creates a BLOCK constraint in the governance graph.
        """
        suspension_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        expires_at = now + timedelta(hours=duration_hours)

        record = SuspensionRecord(
            suspension_id=suspension_id,
            agent_id=agent_id,
            zone_id=zone_id,
            reason=reason,
            severity=severity,
            suspended_at=now,
            expires_at=expires_at,
        )

        # Persist suspension
        await self._persist_suspension(record)

        # Create BLOCK constraint
        if self._graph_service is not None:
            await self._graph_service.add_constraint(
                from_agent=agent_id,
                to_agent="*",
                zone_id=zone_id,
                constraint_type=ConstraintType.BLOCK,
                reason=f"Suspended: {reason}",
            )

        if logger.isEnabledFor(logging.WARNING):
            logger.warning(
                "Suspended agent=%s zone=%s reason=%s (expires=%s)",
                agent_id,
                zone_id,
                reason,
                expires_at,
            )

        return record

    async def appeal_suspension(
        self,
        suspension_id: str,
        reason: str,
    ) -> SuspensionRecord:
        """File an appeal for a suspension.

        Uses the shared ApprovalWorkflow for state management.
        """
        record = await self._get_suspension(suspension_id)
        if record is None:
            msg = f"Suspension {suspension_id!r} not found"
            raise KeyError(msg)

        if record.appeal_status != "none":
            msg = f"Suspension {suspension_id!r} already has appeal status: {record.appeal_status}"
            raise ValueError(msg)

        # Submit appeal via workflow
        self._appeal_workflow.submit(
            submitted_by=record.agent_id,
            record_id=suspension_id,
            metadata={"reason": reason},
        )

        # Update suspension record
        updated = SuspensionRecord(
            suspension_id=record.suspension_id,
            agent_id=record.agent_id,
            zone_id=record.zone_id,
            reason=record.reason,
            severity=record.severity,
            suspended_at=record.suspended_at,
            expires_at=record.expires_at,
            appeal_status="pending",
            appeal_reason=reason,
            appealed_at=datetime.now(UTC),
        )

        await self._update_suspension(updated)
        return updated

    async def decide_appeal(
        self,
        suspension_id: str,
        approved: bool,
        decided_by: str,
    ) -> SuspensionRecord:
        """Decide on a suspension appeal.

        If approved: remove BLOCK constraint, set appeal_status=approved.
        If rejected: set appeal_status=rejected.
        """
        record = await self._get_suspension(suspension_id)
        if record is None:
            msg = f"Suspension {suspension_id!r} not found"
            raise KeyError(msg)

        if record.appeal_status != "pending":
            msg = f"No pending appeal for suspension {suspension_id!r}"
            raise ValueError(msg)

        now = datetime.now(UTC)
        new_status = "approved" if approved else "rejected"

        # Update approval workflow
        if approved:
            self._appeal_workflow.approve(suspension_id, decided_by)
        else:
            self._appeal_workflow.reject(suspension_id, decided_by)

        # Update suspension
        updated = SuspensionRecord(
            suspension_id=record.suspension_id,
            agent_id=record.agent_id,
            zone_id=record.zone_id,
            reason=record.reason,
            severity=record.severity,
            suspended_at=record.suspended_at,
            expires_at=record.expires_at,
            appeal_status=new_status,
            appeal_reason=record.appeal_reason,
            appealed_at=record.appealed_at,
            decided_by=decided_by,
            decided_at=now,
        )

        await self._update_suspension(updated)

        # If approved, remove BLOCK constraint
        if approved and self._graph_service is not None:
            constraints = await self._graph_service.list_constraints(
                zone_id=record.zone_id, agent_id=record.agent_id
            )
            for c in constraints:
                metadata = c.metadata or {}
                if str(metadata.get("constraint_type", "")) == ConstraintType.BLOCK:
                    await self._graph_service.remove_constraint(c.edge_id)

        if logger.isEnabledFor(logging.INFO):
            logger.info(
                "Appeal %s for suspension=%s by=%s",
                new_status,
                suspension_id,
                decided_by,
            )

        return updated

    async def list_suspensions(
        self,
        zone_id: str,
        agent_id: str | None = None,
    ) -> list[SuspensionRecord]:
        """List suspensions, optionally filtered by agent."""
        from sqlalchemy import select

        from nexus.governance.db_models import SuspensionModel

        async with self._session_factory() as session:
            stmt = select(SuspensionModel).where(
                SuspensionModel.zone_id == zone_id,
            )
            if agent_id is not None:
                stmt = stmt.where(SuspensionModel.agent_id == agent_id)

            stmt = stmt.order_by(SuspensionModel.suspended_at.desc())
            result = await session.execute(stmt)
            models = result.scalars().all()

            return [_suspension_model_to_domain(m) for m in models]

    async def _get_suspension(self, suspension_id: str) -> SuspensionRecord | None:
        """Get a suspension by ID."""
        from sqlalchemy import select

        from nexus.governance.db_models import SuspensionModel

        async with self._session_factory() as session:
            stmt = select(SuspensionModel).where(SuspensionModel.id == suspension_id)
            result = await session.execute(stmt)
            model = result.scalar_one_or_none()

        if model is None:
            return None

        return _suspension_model_to_domain(model)

    async def _persist_suspension(self, record: SuspensionRecord) -> None:
        """Persist a suspension record to database."""
        from nexus.governance.db_models import SuspensionModel

        model = SuspensionModel(
            id=record.suspension_id,
            agent_id=record.agent_id,
            zone_id=record.zone_id,
            reason=record.reason,
            severity=record.severity,
            suspended_at=record.suspended_at or datetime.now(UTC),
            expires_at=record.expires_at,
            appeal_status=record.appeal_status,
        )

        async with self._session_factory() as session, session.begin():
            session.add(model)

    async def _update_suspension(self, record: SuspensionRecord) -> None:
        """Update a suspension record in database."""
        from sqlalchemy import select

        from nexus.governance.db_models import SuspensionModel

        async with self._session_factory() as session, session.begin():
            stmt = select(SuspensionModel).where(
                SuspensionModel.id == record.suspension_id,
            )
            result = await session.execute(stmt)
            model = result.scalar_one_or_none()
            if model is None:
                return

            model.appeal_status = record.appeal_status
            model.appeal_reason = record.appeal_reason
            model.appealed_at = record.appealed_at
            model.decided_by = record.decided_by
            model.decided_at = record.decided_at
            await session.flush()

    async def _persist_throttle(self, throttle: ThrottleConfig) -> None:
        """Persist a throttle configuration to database."""
        from nexus.governance.db_models import ThrottleModel

        config_data = {
            "max_tx_per_hour": throttle.max_tx_per_hour,
            "max_amount_per_day": throttle.max_amount_per_day,
            "reason": throttle.reason,
        }

        model = ThrottleModel(
            agent_id=throttle.agent_id,
            zone_id=throttle.zone_id,
            config=json.dumps(config_data),
            applied_at=throttle.applied_at or datetime.now(UTC),
            expires_at=throttle.expires_at,
        )

        async with self._session_factory() as session, session.begin():
            session.add(model)


def _suspension_model_to_domain(model: Any) -> SuspensionRecord:
    """Convert SuspensionModel to domain SuspensionRecord."""
    return SuspensionRecord(
        suspension_id=model.id,
        agent_id=model.agent_id,
        zone_id=model.zone_id,
        reason=model.reason,
        severity=AnomalySeverity(model.severity),
        suspended_at=model.suspended_at,
        expires_at=model.expires_at,
        appeal_status=model.appeal_status,
        appeal_reason=model.appeal_reason,
        appealed_at=model.appealed_at,
        decided_by=model.decided_by,
        decided_at=model.decided_at,
    )
