"""Tests for AnomalyService.

Issue #1359 Phase 1: Service logic tests with mocks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.services.governance.anomaly_service import AnomalyService, StatisticalAnomalyDetector
from nexus.services.governance.models import (
    AgentBaseline,
    AnomalyAlert,
    TransactionSummary,
)


@pytest.fixture
def detector() -> StatisticalAnomalyDetector:
    """Statistical detector with a test baseline."""
    d = StatisticalAnomalyDetector()
    d.set_baseline(
        "agent-1",
        "zone-1",
        AgentBaseline(
            agent_id="agent-1",
            zone_id="zone-1",
            mean_amount=100.0,
            std_amount=10.0,
            mean_frequency=5.0,
            counterparty_count=10,
            computed_at=datetime.now(UTC),
            observation_count=50,
        ),
    )
    d.set_counterparties("agent-1", "zone-1", {"known-a", "known-b"})
    return d


class TestStatisticalAnomalyDetector:
    """Tests for the default detector implementation."""

    def test_normal_transaction(self, detector: StatisticalAnomalyDetector) -> None:
        tx = TransactionSummary("agent-1", "zone-1", 105.0, "known-a", datetime.now(UTC))
        alerts = detector.detect(tx)
        assert len(alerts) == 0

    def test_amount_anomaly(self, detector: StatisticalAnomalyDetector) -> None:
        tx = TransactionSummary("agent-1", "zone-1", 200.0, "known-a", datetime.now(UTC))
        alerts = detector.detect(tx)
        amount_alerts = [a for a in alerts if a.alert_type == "amount"]
        assert len(amount_alerts) >= 1

    def test_counterparty_anomaly(self, detector: StatisticalAnomalyDetector) -> None:
        tx = TransactionSummary("agent-1", "zone-1", 100.0, "unknown-x", datetime.now(UTC))
        alerts = detector.detect(tx)
        cp_alerts = [a for a in alerts if a.alert_type == "counterparty"]
        assert len(cp_alerts) == 1

    def test_no_baseline(self) -> None:
        detector = StatisticalAnomalyDetector()
        tx = TransactionSummary("agent-2", "zone-1", 1000.0, "someone", datetime.now(UTC))
        alerts = detector.detect(tx)
        # No baseline = no amount anomaly
        amount_alerts = [a for a in alerts if a.alert_type == "amount"]
        assert len(amount_alerts) == 0

    def test_combined_anomalies(self, detector: StatisticalAnomalyDetector) -> None:
        tx = TransactionSummary("agent-1", "zone-1", 200.0, "unknown-x", datetime.now(UTC))
        alerts = detector.detect(tx)
        # Both amount and counterparty anomalies
        assert len(alerts) >= 2


class TestAnomalyService:
    """Tests for the AnomalyService orchestrator."""

    @pytest.fixture
    def mock_session_factory(self) -> AsyncMock:
        """Mock async session factory."""
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(
            return_value=AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock())
        )

        factory = MagicMock(return_value=session)
        return factory

    @pytest.fixture
    def service(
        self,
        mock_session_factory: AsyncMock,
        detector: StatisticalAnomalyDetector,
    ) -> AnomalyService:
        return AnomalyService(
            session_factory=mock_session_factory,
            detector=detector,
        )

    @pytest.mark.asyncio
    async def test_analyze_normal_transaction(self, service: AnomalyService) -> None:
        alerts = await service.analyze_transaction(
            agent_id="agent-1",
            zone_id="zone-1",
            amount=105.0,
            to="known-a",
        )
        assert len(alerts) == 0

    @pytest.mark.asyncio
    async def test_analyze_anomalous_transaction(self, service: AnomalyService) -> None:
        alerts = await service.analyze_transaction(
            agent_id="agent-1",
            zone_id="zone-1",
            amount=200.0,
            to="known-a",
        )
        assert len(alerts) >= 1
        assert all(isinstance(a, AnomalyAlert) for a in alerts)
