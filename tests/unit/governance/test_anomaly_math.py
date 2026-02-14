"""Tests for anomaly math pure functions.

Issue #1359 Phase 1: Z-score, IQR, baseline computation.
Parametrized with known values, edge cases.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nexus.governance.anomaly_math import (
    compute_baseline,
    compute_iqr_bounds,
    compute_z_score,
    detect_amount_anomaly,
    detect_counterparty_anomaly,
    detect_frequency_anomaly,
)
from nexus.governance.models import (
    AgentBaseline,
    AnomalyDetectionConfig,
    AnomalySeverity,
    TransactionSummary,
)


class TestComputeZScore:
    """Tests for Z-score computation."""

    def test_z_score_basic(self) -> None:
        assert compute_z_score(10.0, 5.0, 2.5) == 2.0

    def test_z_score_negative(self) -> None:
        assert compute_z_score(0.0, 5.0, 2.5) == -2.0

    def test_z_score_at_mean(self) -> None:
        assert compute_z_score(5.0, 5.0, 2.5) == 0.0

    def test_z_score_zero_std(self) -> None:
        assert compute_z_score(10.0, 5.0, 0.0) == 0.0

    def test_z_score_negative_std(self) -> None:
        assert compute_z_score(10.0, 5.0, -1.0) == 0.0


class TestComputeIQRBounds:
    """Tests for IQR bound computation."""

    def test_basic_iqr(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
        lower, upper = compute_iqr_bounds(values)
        assert lower < 1.0
        assert upper > 8.0

    def test_iqr_symmetric(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0]
        lower, upper = compute_iqr_bounds(values)
        assert isinstance(lower, float)
        assert isinstance(upper, float)
        assert lower < upper

    def test_iqr_too_few_values(self) -> None:
        with pytest.raises(ValueError, match="at least 4"):
            compute_iqr_bounds([1.0, 2.0, 3.0])

    def test_iqr_identical_values(self) -> None:
        values = [5.0, 5.0, 5.0, 5.0]
        lower, upper = compute_iqr_bounds(values)
        assert lower == 5.0
        assert upper == 5.0

    def test_iqr_large_spread(self) -> None:
        values = [1.0, 2.0, 100.0, 200.0]
        lower, upper = compute_iqr_bounds(values)
        assert lower < 1.0
        assert upper > 200.0


class TestComputeBaseline:
    """Tests for baseline computation."""

    def test_empty_transactions(self) -> None:
        baseline = compute_baseline([], agent_id="a1", zone_id="z1")
        assert baseline.mean_amount == 0.0
        assert baseline.std_amount == 0.0
        assert baseline.observation_count == 0

    def test_single_transaction(self) -> None:
        now = datetime.now(UTC)
        txs = [TransactionSummary("a1", "z1", 100.0, "b1", now)]
        baseline = compute_baseline(txs, agent_id="a1", zone_id="z1")
        assert baseline.mean_amount == 100.0
        assert baseline.std_amount == 0.0
        assert baseline.counterparty_count == 1
        assert baseline.observation_count == 1

    def test_multiple_transactions(self) -> None:
        now = datetime.now(UTC)
        txs = [
            TransactionSummary("a1", "z1", 10.0, "b1", now - timedelta(days=2)),
            TransactionSummary("a1", "z1", 20.0, "b2", now - timedelta(days=1)),
            TransactionSummary("a1", "z1", 30.0, "b1", now),
        ]
        baseline = compute_baseline(txs, agent_id="a1", zone_id="z1")
        assert baseline.mean_amount == 20.0
        assert baseline.counterparty_count == 2
        assert baseline.observation_count == 3
        assert baseline.mean_frequency > 0

    def test_baseline_frequency_calculation(self) -> None:
        now = datetime.now(UTC)
        txs = [
            TransactionSummary("a1", "z1", 10.0, "b1", now - timedelta(days=10)),
            TransactionSummary("a1", "z1", 20.0, "b2", now),
        ]
        baseline = compute_baseline(txs, agent_id="a1", zone_id="z1")
        assert baseline.mean_frequency == pytest.approx(0.2, abs=0.01)


class TestDetectAmountAnomaly:
    """Tests for amount anomaly detection."""

    @pytest.fixture
    def baseline(self) -> AgentBaseline:
        return AgentBaseline(
            agent_id="a1",
            zone_id="z1",
            mean_amount=100.0,
            std_amount=10.0,
            mean_frequency=5.0,
            counterparty_count=10,
            computed_at=datetime.now(UTC),
            observation_count=50,
        )

    @pytest.fixture
    def config(self) -> AnomalyDetectionConfig:
        return AnomalyDetectionConfig(z_score_threshold=3.0, min_observations=10)

    def test_normal_amount(self, baseline: AgentBaseline, config: AnomalyDetectionConfig) -> None:
        result = detect_amount_anomaly(105.0, baseline, config)
        assert result is None

    def test_anomalous_amount(
        self, baseline: AgentBaseline, config: AnomalyDetectionConfig
    ) -> None:
        result = detect_amount_anomaly(200.0, baseline, config)
        assert result is not None
        assert result.alert_type == "amount"
        assert result.agent_id == "a1"

    def test_too_few_observations(self, config: AnomalyDetectionConfig) -> None:
        baseline = AgentBaseline(
            agent_id="a1",
            zone_id="z1",
            mean_amount=100.0,
            std_amount=10.0,
            mean_frequency=5.0,
            counterparty_count=3,
            computed_at=datetime.now(UTC),
            observation_count=5,
        )
        result = detect_amount_anomaly(200.0, baseline, config)
        assert result is None

    def test_severity_levels(self, baseline: AgentBaseline, config: AnomalyDetectionConfig) -> None:
        # 3 std devs -> LOW
        result = detect_amount_anomaly(131.0, baseline, config)
        assert result is not None
        assert result.severity == AnomalySeverity.LOW

        # 6 std devs -> MEDIUM
        result = detect_amount_anomaly(160.0, baseline, config)
        assert result is not None
        assert result.severity in {AnomalySeverity.MEDIUM, AnomalySeverity.HIGH}

        # 9+ std devs -> CRITICAL
        result = detect_amount_anomaly(200.0, baseline, config)
        assert result is not None
        assert result.severity == AnomalySeverity.CRITICAL


class TestDetectFrequencyAnomaly:
    """Tests for frequency anomaly detection."""

    @pytest.fixture
    def baseline(self) -> AgentBaseline:
        return AgentBaseline(
            agent_id="a1",
            zone_id="z1",
            mean_amount=100.0,
            std_amount=10.0,
            mean_frequency=5.0,
            counterparty_count=10,
            computed_at=datetime.now(UTC),
            observation_count=50,
        )

    @pytest.fixture
    def config(self) -> AnomalyDetectionConfig:
        return AnomalyDetectionConfig(z_score_threshold=3.0, min_observations=10)

    def test_normal_frequency(
        self, baseline: AgentBaseline, config: AnomalyDetectionConfig
    ) -> None:
        result = detect_frequency_anomaly(6, baseline, config)
        assert result is None

    def test_anomalous_frequency(
        self, baseline: AgentBaseline, config: AnomalyDetectionConfig
    ) -> None:
        result = detect_frequency_anomaly(50, baseline, config)
        assert result is not None
        assert result.alert_type == "frequency"

    def test_too_few_observations(self, config: AnomalyDetectionConfig) -> None:
        baseline = AgentBaseline(
            agent_id="a1",
            zone_id="z1",
            mean_amount=100.0,
            std_amount=10.0,
            mean_frequency=5.0,
            counterparty_count=3,
            computed_at=datetime.now(UTC),
            observation_count=5,
        )
        result = detect_frequency_anomaly(50, baseline, config)
        assert result is None


class TestDetectCounterpartyAnomaly:
    """Tests for counterparty anomaly detection."""

    def test_known_counterparty(self) -> None:
        result = detect_counterparty_anomaly("known-agent", {"known-agent", "other"}, "a1", "z1")
        assert result is None

    def test_unknown_counterparty(self) -> None:
        result = detect_counterparty_anomaly("new-agent", {"known-agent", "other"}, "a1", "z1")
        assert result is not None
        assert result.alert_type == "counterparty"
        assert result.severity == AnomalySeverity.LOW

    def test_empty_known_set(self) -> None:
        result = detect_counterparty_anomaly("any", set(), "a1", "z1")
        assert result is not None
