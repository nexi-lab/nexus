"""Anomaly detection service.

Issue #1359 Phase 1: Transaction anomaly detection with statistical methods.
Manages baselines, detects anomalies, records alerts.

Hot path: analyze_transaction() — Z-score vs cached baseline (<1ms).
Background: recompute_baselines() — batch job for baseline refresh.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.governance.anomaly_math import (
    detect_amount_anomaly,
    detect_counterparty_anomaly,
)
from nexus.governance.models import (
    AgentBaseline,
    AnomalyAlert,
    AnomalyDetectionConfig,
    AnomalySeverity,
    TransactionSummary,
)
from nexus.governance.protocols import AnomalyDetectorProtocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class StatisticalAnomalyDetector:
    """Default anomaly detector using Z-score and IQR methods.

    Implements AnomalyDetectorProtocol.
    """

    def __init__(
        self,
        config: AnomalyDetectionConfig | None = None,
        baselines: dict[tuple[str, str], AgentBaseline] | None = None,
        counterparties: dict[tuple[str, str], set[str]] | None = None,
    ) -> None:
        self._config = config or AnomalyDetectionConfig()
        self._baselines = baselines or {}
        self._counterparties = counterparties or {}

    def set_baseline(self, agent_id: str, zone_id: str, baseline: AgentBaseline) -> None:
        """Update cached baseline for an agent."""
        self._baselines[(agent_id, zone_id)] = baseline

    def set_counterparties(self, agent_id: str, zone_id: str, cps: set[str]) -> None:
        """Update known counterparties for an agent."""
        self._counterparties[(agent_id, zone_id)] = cps

    def detect(self, transaction: TransactionSummary) -> list[AnomalyAlert]:
        """Detect anomalies in a transaction.

        Checks amount, frequency (stub), and counterparty anomalies.
        """
        alerts: list[AnomalyAlert] = []
        key = (transaction.agent_id, transaction.zone_id)
        baseline = self._baselines.get(key)

        if baseline is not None:
            # Amount anomaly
            amount_alert = detect_amount_anomaly(transaction.amount, baseline, self._config)
            if amount_alert is not None:
                alerts.append(amount_alert)

        # Counterparty anomaly
        known = self._counterparties.get(key, set())
        if known:
            cp_alert = detect_counterparty_anomaly(
                transaction.counterparty,
                known,
                transaction.agent_id,
                transaction.zone_id,
            )
            if cp_alert is not None:
                alerts.append(cp_alert)

        return alerts


class AnomalyService:
    """Manages anomaly detection lifecycle.

    Responsibilities:
        - Analyze transactions inline (hot path, <1ms)
        - Persist alerts to database
        - Recompute baselines (background batch)
        - Query and resolve alerts
    """

    _BASELINE_CACHE_TTL: float = 60.0  # seconds

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        detector: AnomalyDetectorProtocol | None = None,
        config: AnomalyDetectionConfig | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config or AnomalyDetectionConfig()
        self._detector = detector or StatisticalAnomalyDetector(config=self._config)
        # Cache: (agent_id, zone_id) -> (baseline, expires_at_monotonic)
        self._baseline_cache: dict[tuple[str, str], tuple[AgentBaseline, float]] = {}

    async def analyze_transaction(
        self,
        agent_id: str,
        zone_id: str,
        amount: float,
        to: str,
        timestamp: datetime | None = None,
    ) -> list[AnomalyAlert]:
        """Analyze a transaction for anomalies (hot path).

        Returns alerts if any anomalies detected.
        Alerts are persisted asynchronously.
        """
        ts = timestamp or datetime.now(UTC)
        tx = TransactionSummary(
            agent_id=agent_id,
            zone_id=zone_id,
            amount=amount,
            counterparty=to,
            timestamp=ts,
        )
        alerts = self._detector.detect(tx)

        # Persist alerts (fire-and-forget style but awaited for correctness)
        if alerts:
            await self._persist_alerts(alerts)

        return alerts

    async def get_alerts(
        self,
        zone_id: str,
        severity: AnomalySeverity | None = None,
        resolved: bool | None = None,
    ) -> list[AnomalyAlert]:
        """Query alerts with optional filters."""
        from sqlalchemy import select

        from nexus.governance.db_models import AnomalyAlertModel

        async with self._session_factory() as session:
            stmt = select(AnomalyAlertModel).where(AnomalyAlertModel.zone_id == zone_id)

            if severity is not None:
                stmt = stmt.where(AnomalyAlertModel.severity == severity)
            if resolved is not None:
                stmt = stmt.where(AnomalyAlertModel.resolved == resolved)

            stmt = stmt.order_by(AnomalyAlertModel.created_at.desc())
            result = await session.execute(stmt)
            models = result.scalars().all()

            return [_alert_model_to_domain(m) for m in models]

    async def resolve_alert(
        self,
        alert_id: str,
        resolved_by: str,
    ) -> AnomalyAlert | None:
        """Mark an alert as resolved."""
        from sqlalchemy import select

        from nexus.governance.db_models import AnomalyAlertModel

        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            stmt = select(AnomalyAlertModel).where(AnomalyAlertModel.id == alert_id)
            result = await session.execute(stmt)
            model = result.scalar_one_or_none()
            if model is None:
                return None

            model.resolved = True
            model.resolved_at = now
            model.resolved_by = resolved_by
            await session.flush()

            return _alert_model_to_domain(model)

    async def _persist_alerts(self, alerts: list[AnomalyAlert]) -> None:
        """Persist anomaly alerts to database."""
        from nexus.governance.db_models import AnomalyAlertModel

        try:
            async with self._session_factory() as session, session.begin():
                for alert in alerts:
                    model = AnomalyAlertModel(
                        id=alert.alert_id,
                        agent_id=alert.agent_id,
                        zone_id=alert.zone_id,
                        severity=alert.severity,
                        alert_type=alert.alert_type,
                        details=json.dumps(alert.details) if alert.details else None,
                        transaction_ref=alert.transaction_ref,
                        resolved=False,
                    )
                    session.add(model)
        except Exception:
            logger.exception("Failed to persist anomaly alerts")


def _alert_model_to_domain(model: Any) -> AnomalyAlert:
    """Convert AnomalyAlertModel to domain AnomalyAlert."""
    details: dict[str, object] = {}
    if hasattr(model, "details") and model.details:
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            details = json.loads(model.details)

    return AnomalyAlert(
        alert_id=model.id,
        agent_id=model.agent_id,
        zone_id=model.zone_id,
        severity=AnomalySeverity(model.severity),
        alert_type=model.alert_type,
        details=details,
        transaction_ref=model.transaction_ref,
        created_at=model.created_at,
        resolved=model.resolved,
        resolved_at=model.resolved_at,
        resolved_by=model.resolved_by,
    )
