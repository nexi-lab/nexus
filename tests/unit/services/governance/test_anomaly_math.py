"""Unit tests for anomaly detection pure math functions.

Tests compute_z_score, compute_iqr_bounds, compute_baseline,
detect_amount_anomaly, detect_frequency_anomaly, detect_counterparty_anomaly,
and the internal _z_to_severity mapping.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from nexus.services.governance.anomaly_math import (
    compute_baseline,
    compute_iqr_bounds,
    compute_z_score,
    detect_amount_anomaly,
    detect_counterparty_anomaly,
    detect_frequency_anomaly,
)
from nexus.services.governance.models import (
    AgentBaseline,
    AnomalyDetectionConfig,
    AnomalySeverity,
    TransactionSummary,
)

# ---------------------------------------------------------------------------
# compute_z_score
# ---------------------------------------------------------------------------


class TestComputeZScore:
    """Tests for compute_z_score."""

    def test_positive_z_score(self) -> None:
        result = compute_z_score(value=12.0, mean=10.0, std=2.0)
        assert result == pytest.approx(1.0)

    def test_negative_z_score(self) -> None:
        result = compute_z_score(value=8.0, mean=10.0, std=2.0)
        assert result == pytest.approx(-1.0)

    def test_zero_std_returns_zero(self) -> None:
        result = compute_z_score(value=100.0, mean=10.0, std=0.0)
        assert result == 0.0

    def test_negative_std_returns_zero(self) -> None:
        result = compute_z_score(value=100.0, mean=10.0, std=-5.0)
        assert result == 0.0

    def test_value_equals_mean(self) -> None:
        result = compute_z_score(value=10.0, mean=10.0, std=3.0)
        assert result == pytest.approx(0.0)

    def test_large_z_score(self) -> None:
        result = compute_z_score(value=110.0, mean=10.0, std=10.0)
        assert result == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# compute_iqr_bounds
# ---------------------------------------------------------------------------


class TestComputeIqrBounds:
    """Tests for compute_iqr_bounds."""

    def test_basic_iqr(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
        lower, upper = compute_iqr_bounds(values)
        # Q1 ~ 2.5, Q3 ~ 6.5, IQR = 4.0
        # lower = Q1 - 1.5*IQR, upper = Q3 + 1.5*IQR
        assert lower < upper
        assert isinstance(lower, float)
        assert isinstance(upper, float)

    def test_minimum_four_values(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0]
        lower, upper = compute_iqr_bounds(values)
        assert lower < upper

    def test_fewer_than_four_raises(self) -> None:
        with pytest.raises(ValueError, match="at least 4"):
            compute_iqr_bounds([1.0, 2.0, 3.0])

    def test_empty_list_raises(self) -> None:
        with pytest.raises(ValueError, match="at least 4"):
            compute_iqr_bounds([])

    def test_identical_values(self) -> None:
        values = [5.0, 5.0, 5.0, 5.0]
        lower, upper = compute_iqr_bounds(values)
        # IQR = 0, so bounds are Q1 and Q3 (both 5.0)
        assert lower == pytest.approx(5.0)
        assert upper == pytest.approx(5.0)

    def test_unsorted_input(self) -> None:
        values = [8.0, 2.0, 6.0, 4.0, 1.0, 3.0, 7.0, 5.0]
        lower, upper = compute_iqr_bounds(values)
        # Should sort internally, same result as sorted
        lower2, upper2 = compute_iqr_bounds(sorted(values))
        assert lower == pytest.approx(lower2)
        assert upper == pytest.approx(upper2)


# ---------------------------------------------------------------------------
# compute_baseline
# ---------------------------------------------------------------------------


class TestComputeBaseline:
    """Tests for compute_baseline."""

    def test_empty_transactions(self) -> None:
        baseline = compute_baseline([], "agent-1", "zone-1")
        assert baseline.agent_id == "agent-1"
        assert baseline.zone_id == "zone-1"
        assert baseline.mean_amount == 0.0
        assert baseline.std_amount == 0.0
        assert baseline.mean_frequency == 0.0
        assert baseline.counterparty_count == 0
        assert baseline.observation_count == 0

    def test_single_transaction(self) -> None:
        now = datetime.now(UTC)
        txs = [
            TransactionSummary(
                agent_id="a1",
                zone_id="z1",
                amount=100.0,
                counterparty="cp1",
                timestamp=now,
            )
        ]
        baseline = compute_baseline(txs, "a1", "z1")
        assert baseline.mean_amount == pytest.approx(100.0)
        assert baseline.std_amount == pytest.approx(0.0)
        assert baseline.mean_frequency == pytest.approx(1.0)
        assert baseline.counterparty_count == 1
        assert baseline.observation_count == 1

    def test_multiple_transactions(self) -> None:
        now = datetime.now(UTC)
        txs = [
            TransactionSummary("a1", "z1", 100.0, "cp1", now - timedelta(days=2)),
            TransactionSummary("a1", "z1", 200.0, "cp2", now - timedelta(days=1)),
            TransactionSummary("a1", "z1", 300.0, "cp1", now),
        ]
        baseline = compute_baseline(txs, "a1", "z1")
        assert baseline.mean_amount == pytest.approx(200.0)
        assert baseline.std_amount > 0
        assert baseline.counterparty_count == 2
        assert baseline.observation_count == 3
        # Frequency: 3 transactions over 2 days = 1.5/day
        assert baseline.mean_frequency == pytest.approx(1.5)

    def test_std_computation(self) -> None:
        now = datetime.now(UTC)
        txs = [
            TransactionSummary("a1", "z1", 10.0, "cp1", now),
            TransactionSummary("a1", "z1", 20.0, "cp1", now),
        ]
        baseline = compute_baseline(txs, "a1", "z1")
        mean = 15.0
        variance = ((10.0 - mean) ** 2 + (20.0 - mean) ** 2) / 2
        expected_std = math.sqrt(variance)
        assert baseline.std_amount == pytest.approx(expected_std)


# ---------------------------------------------------------------------------
# detect_amount_anomaly
# ---------------------------------------------------------------------------


class TestDetectAmountAnomaly:
    """Tests for detect_amount_anomaly."""

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

    @pytest.fixture()
    def config(self) -> AnomalyDetectionConfig:
        return AnomalyDetectionConfig(z_score_threshold=3.0, min_observations=10)

    def test_normal_amount_no_alert(
        self, baseline: AgentBaseline, config: AnomalyDetectionConfig
    ) -> None:
        alert = detect_amount_anomaly(105.0, baseline, config)
        assert alert is None

    def test_anomalous_amount_triggers_alert(
        self, baseline: AgentBaseline, config: AnomalyDetectionConfig
    ) -> None:
        # z-score = (200 - 100) / 10 = 10.0, well above threshold of 3.0
        alert = detect_amount_anomaly(200.0, baseline, config)
        assert alert is not None
        assert alert.alert_type == "amount"
        assert alert.agent_id == "a1"
        assert alert.zone_id == "z1"

    def test_insufficient_observations_returns_none(self, config: AnomalyDetectionConfig) -> None:
        baseline = AgentBaseline(
            agent_id="a1",
            zone_id="z1",
            mean_amount=100.0,
            std_amount=10.0,
            mean_frequency=5.0,
            counterparty_count=3,
            computed_at=datetime.now(UTC),
            observation_count=5,  # Below min_observations=10
        )
        alert = detect_amount_anomaly(200.0, baseline, config)
        assert alert is None

    def test_severity_scales_with_z_score(self, baseline: AgentBaseline) -> None:
        config = AnomalyDetectionConfig(z_score_threshold=2.0, min_observations=10)
        # z = (200 - 100) / 10 = 10.0, threshold=2.0
        # 10.0 >= 2.0 * 3 = 6.0 -> CRITICAL
        alert = detect_amount_anomaly(200.0, baseline, config)
        assert alert is not None
        assert alert.severity == AnomalySeverity.CRITICAL

    def test_low_severity_at_threshold(self, baseline: AgentBaseline) -> None:
        config = AnomalyDetectionConfig(z_score_threshold=3.0, min_observations=10)
        # z = (131 - 100) / 10 = 3.1, just above threshold
        alert = detect_amount_anomaly(131.0, baseline, config)
        assert alert is not None
        assert alert.severity == AnomalySeverity.LOW


# ---------------------------------------------------------------------------
# detect_frequency_anomaly
# ---------------------------------------------------------------------------


class TestDetectFrequencyAnomaly:
    """Tests for detect_frequency_anomaly."""

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

    @pytest.fixture()
    def config(self) -> AnomalyDetectionConfig:
        return AnomalyDetectionConfig(z_score_threshold=3.0, min_observations=10)

    def test_normal_frequency_no_alert(
        self, baseline: AgentBaseline, config: AnomalyDetectionConfig
    ) -> None:
        alert = detect_frequency_anomaly(6, baseline, config)
        assert alert is None

    def test_anomalous_frequency_triggers_alert(
        self, baseline: AgentBaseline, config: AnomalyDetectionConfig
    ) -> None:
        # Very high count relative to baseline mean_frequency=5.0
        alert = detect_frequency_anomaly(100, baseline, config)
        assert alert is not None
        assert alert.alert_type == "frequency"

    def test_insufficient_observations_returns_none(self, config: AnomalyDetectionConfig) -> None:
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
        alert = detect_frequency_anomaly(100, baseline, config)
        assert alert is None

    def test_zero_mean_frequency_returns_none(self, config: AnomalyDetectionConfig) -> None:
        baseline = AgentBaseline(
            agent_id="a1",
            zone_id="z1",
            mean_amount=100.0,
            std_amount=10.0,
            mean_frequency=0.0,
            counterparty_count=3,
            computed_at=datetime.now(UTC),
            observation_count=50,
        )
        alert = detect_frequency_anomaly(100, baseline, config)
        assert alert is None


# ---------------------------------------------------------------------------
# detect_counterparty_anomaly
# ---------------------------------------------------------------------------


class TestDetectCounterpartyAnomaly:
    """Tests for detect_counterparty_anomaly."""

    def test_known_counterparty_no_alert(self) -> None:
        known = {"cp1", "cp2", "cp3"}
        alert = detect_counterparty_anomaly("cp1", known, "a1", "z1")
        assert alert is None

    def test_unknown_counterparty_triggers_alert(self) -> None:
        known = {"cp1", "cp2", "cp3"}
        alert = detect_counterparty_anomaly("cp_new", known, "a1", "z1")
        assert alert is not None
        assert alert.alert_type == "counterparty"
        assert alert.severity == AnomalySeverity.LOW
        assert alert.details["new_counterparty"] == "cp_new"
        assert alert.details["known_count"] == 3

    def test_empty_known_set_triggers_alert(self) -> None:
        # Empty set: any counterparty is "new" since none are known
        # The function checks membership, so "cp1" not in set() => alert
        alert = detect_counterparty_anomaly("cp1", set(), "a1", "z1")
        assert alert is not None
        assert alert.alert_type == "counterparty"
        assert alert.details["known_count"] == 0
