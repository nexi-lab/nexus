"""Unit tests for AnomalyService and StatisticalAnomalyDetector.

Tests the detector's detect() method and the service's
analyze_transaction() orchestration with mocked dependencies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.governance.anomaly_service import (
    AnomalyService,
    StatisticalAnomalyDetector,
)
from nexus.bricks.governance.models import (
    AgentBaseline,
    AnomalyDetectionConfig,
    AnomalySeverity,
    TransactionSummary,
)

# ---------------------------------------------------------------------------
# StatisticalAnomalyDetector
# ---------------------------------------------------------------------------


class TestStatisticalAnomalyDetector:
    """Tests for the StatisticalAnomalyDetector."""

    @pytest.fixture()
    def detector(self) -> StatisticalAnomalyDetector:
        return StatisticalAnomalyDetector(
            config=AnomalyDetectionConfig(z_score_threshold=2.0, min_observations=5),
        )

    @pytest.fixture()
    def baseline(self) -> AgentBaseline:
        return AgentBaseline(
            agent_id="a1",
            zone_id="z1",
            mean_amount=100.0,
            std_amount=10.0,
            mean_frequency=5.0,
            counterparty_count=3,
            computed_at=datetime.now(UTC),
            observation_count=50,
        )

    def test_no_baseline_no_amount_alert(self, detector: StatisticalAnomalyDetector) -> None:
        tx = TransactionSummary("a1", "z1", 999.0, "cp1", datetime.now(UTC))
        alerts = detector.detect(tx)
        assert len(alerts) == 0

    def test_amount_anomaly_detected(
        self,
        detector: StatisticalAnomalyDetector,
        baseline: AgentBaseline,
    ) -> None:
        detector.set_baseline("a1", "z1", baseline)
        # z = (200 - 100) / 10 = 10.0, threshold 2.0 -> alert
        tx = TransactionSummary("a1", "z1", 200.0, "cp1", datetime.now(UTC))
        alerts = detector.detect(tx)
        amount_alerts = [a for a in alerts if a.alert_type == "amount"]
        assert len(amount_alerts) == 1

    def test_normal_amount_no_alert(
        self,
        detector: StatisticalAnomalyDetector,
        baseline: AgentBaseline,
    ) -> None:
        detector.set_baseline("a1", "z1", baseline)
        # z = (105 - 100) / 10 = 0.5, below threshold
        tx = TransactionSummary("a1", "z1", 105.0, "cp1", datetime.now(UTC))
        alerts = detector.detect(tx)
        amount_alerts = [a for a in alerts if a.alert_type == "amount"]
        assert len(amount_alerts) == 0

    def test_counterparty_anomaly_detected(self, detector: StatisticalAnomalyDetector) -> None:
        detector.set_counterparties("a1", "z1", {"cp1", "cp2"})
        tx = TransactionSummary("a1", "z1", 100.0, "cp_unknown", datetime.now(UTC))
        alerts = detector.detect(tx)
        cp_alerts = [a for a in alerts if a.alert_type == "counterparty"]
        assert len(cp_alerts) == 1

    def test_known_counterparty_no_alert(self, detector: StatisticalAnomalyDetector) -> None:
        detector.set_counterparties("a1", "z1", {"cp1", "cp2"})
        tx = TransactionSummary("a1", "z1", 100.0, "cp1", datetime.now(UTC))
        alerts = detector.detect(tx)
        cp_alerts = [a for a in alerts if a.alert_type == "counterparty"]
        assert len(cp_alerts) == 0

    def test_empty_counterparties_no_alert(self, detector: StatisticalAnomalyDetector) -> None:
        # No counterparties set -> no counterparty check performed
        tx = TransactionSummary("a1", "z1", 100.0, "cp_unknown", datetime.now(UTC))
        alerts = detector.detect(tx)
        cp_alerts = [a for a in alerts if a.alert_type == "counterparty"]
        assert len(cp_alerts) == 0

    def test_both_amount_and_counterparty_alerts(
        self,
        detector: StatisticalAnomalyDetector,
        baseline: AgentBaseline,
    ) -> None:
        detector.set_baseline("a1", "z1", baseline)
        detector.set_counterparties("a1", "z1", {"cp1"})
        # Amount anomaly + unknown counterparty
        tx = TransactionSummary("a1", "z1", 200.0, "cp_new", datetime.now(UTC))
        alerts = detector.detect(tx)
        types = {a.alert_type for a in alerts}
        assert "amount" in types
        assert "counterparty" in types


# ---------------------------------------------------------------------------
# AnomalyService.analyze_transaction
# ---------------------------------------------------------------------------


class TestAnomalyServiceAnalyzeTransaction:
    """Tests for AnomalyService.analyze_transaction orchestration."""

    @pytest.mark.asyncio
    async def test_delegates_to_detector(self) -> None:
        mock_detector = MagicMock()
        mock_detector.detect.return_value = []

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        session_factory = AsyncMock(return_value=mock_session)

        svc = AnomalyService(session_factory=session_factory, detector=mock_detector)
        alerts = await svc.analyze_transaction("a1", "z1", 100.0, "cp1")
        assert alerts == []
        mock_detector.detect.assert_called_once()

    @pytest.mark.asyncio
    async def test_persists_alerts_when_present(self) -> None:
        from nexus.bricks.governance.models import AnomalyAlert

        alert = AnomalyAlert(
            alert_id="alert-1",
            agent_id="a1",
            zone_id="z1",
            severity=AnomalySeverity.HIGH,
            alert_type="amount",
        )
        mock_detector = MagicMock()
        mock_detector.detect.return_value = [alert]

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            def begin(self):
                return self

            def add(self, m):
                pass

        svc = AnomalyService(
            session_factory=lambda: _FakeSession(),
            detector=mock_detector,
        )
        result = await svc.analyze_transaction("a1", "z1", 100.0, "cp1")
        assert len(result) == 1
        assert result[0].alert_id == "alert-1"

    @pytest.mark.asyncio
    async def test_no_persist_when_no_alerts(self) -> None:
        mock_detector = MagicMock()
        mock_detector.detect.return_value = []

        session_factory = AsyncMock()
        svc = AnomalyService(session_factory=session_factory, detector=mock_detector)
        alerts = await svc.analyze_transaction("a1", "z1", 100.0, "cp1")
        assert alerts == []
        # session_factory should not be called since no alerts to persist
        session_factory.assert_not_called()
