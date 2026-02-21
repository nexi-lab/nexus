"""Unit tests for anomaly detection pure math functions.

Issue #2129 §9A: Tests for compute_z_score, compute_iqr_bounds,
compute_baseline, detect_amount_anomaly, detect_frequency_anomaly,
detect_counterparty_anomaly, and _z_to_severity.
"""

from datetime import UTC, datetime

import pytest

from nexus.bricks.governance.anomaly_math import (
    _z_to_severity,
    compute_baseline,
    compute_iqr_bounds,
    compute_z_score,
    detect_amount_anomaly,
    detect_counterparty_anomaly,
    detect_frequency_anomaly,
)
from nexus.bricks.governance.models import (
    AgentBaseline,
    AnomalyDetectionConfig,
    AnomalySeverity,
    TransactionSummary,
)

NOW = datetime(2024, 6, 1, tzinfo=UTC)

# ── compute_z_score ─────────────────────────────────────────────────────


class TestComputeZScore:
    def test_zero_std_returns_zero(self) -> None:
        assert compute_z_score(100.0, 50.0, 0.0) == 0.0

    def test_negative_std_returns_zero(self) -> None:
        assert compute_z_score(100.0, 50.0, -1.0) == 0.0

    def test_positive_deviation(self) -> None:
        assert compute_z_score(80.0, 50.0, 10.0) == pytest.approx(3.0)

    def test_negative_deviation(self) -> None:
        assert compute_z_score(20.0, 50.0, 10.0) == pytest.approx(-3.0)

    def test_zero_deviation(self) -> None:
        assert compute_z_score(50.0, 50.0, 10.0) == 0.0


# ── compute_iqr_bounds ──────────────────────────────────────────────────


class TestComputeIqrBounds:
    def test_too_few_values_raises(self) -> None:
        with pytest.raises(ValueError, match="at least 4"):
            compute_iqr_bounds([1.0, 2.0, 3.0])

    def test_minimum_four_values(self) -> None:
        lower, upper = compute_iqr_bounds([1.0, 2.0, 3.0, 4.0])
        assert lower < upper

    def test_sorted_and_unsorted_same_result(self) -> None:
        vals = [10.0, 1.0, 5.0, 3.0, 7.0, 9.0]
        a = compute_iqr_bounds(vals)
        b = compute_iqr_bounds(sorted(vals))
        assert a[0] == pytest.approx(b[0])
        assert a[1] == pytest.approx(b[1])

    def test_uniform_values_narrow_bounds(self) -> None:
        lower, upper = compute_iqr_bounds([5.0, 5.0, 5.0, 5.0])
        # All same → IQR=0 → bounds collapse to the value
        assert lower == pytest.approx(5.0)
        assert upper == pytest.approx(5.0)


# ── compute_baseline ────────────────────────────────────────────────────


class TestComputeBaseline:
    def test_empty_transactions(self) -> None:
        b = compute_baseline([], "agent1", "zone1")
        assert b.mean_amount == 0.0
        assert b.std_amount == 0.0
        assert b.observation_count == 0

    def test_single_transaction(self) -> None:
        txs = [TransactionSummary("a1", "z1", 100.0, "cp1", NOW)]
        b = compute_baseline(txs, "a1", "z1")
        assert b.mean_amount == 100.0
        assert b.std_amount == 0.0
        assert b.observation_count == 1
        assert b.counterparty_count == 1
        assert b.mean_frequency == 1.0  # single tx → default 1/day

    def test_multi_day_span(self) -> None:
        txs = [
            TransactionSummary("a1", "z1", 100.0, "cp1", datetime(2024, 6, 1, tzinfo=UTC)),
            TransactionSummary("a1", "z1", 200.0, "cp2", datetime(2024, 6, 3, tzinfo=UTC)),
        ]
        b = compute_baseline(txs, "a1", "z1")
        assert b.mean_amount == pytest.approx(150.0)
        assert b.observation_count == 2
        assert b.counterparty_count == 2
        # 2 txs over 2 days = 1.0/day
        assert b.mean_frequency == pytest.approx(1.0)


# ── detect_amount_anomaly ───────────────────────────────────────────────


class TestDetectAmountAnomaly:
    @pytest.fixture()
    def baseline(self) -> AgentBaseline:
        return AgentBaseline(
            agent_id="a1",
            zone_id="z1",
            mean_amount=100.0,
            std_amount=10.0,
            mean_frequency=5.0,
            counterparty_count=3,
            computed_at=NOW,
            observation_count=20,
        )

    @pytest.fixture()
    def config(self) -> AnomalyDetectionConfig:
        return AnomalyDetectionConfig(z_score_threshold=3.0, min_observations=10)

    def test_below_threshold_returns_none(
        self, baseline: AgentBaseline, config: AnomalyDetectionConfig
    ) -> None:
        # z = (120 - 100)/10 = 2.0 < 3.0
        assert detect_amount_anomaly(120.0, baseline, config) is None

    def test_above_threshold_returns_alert(
        self, baseline: AgentBaseline, config: AnomalyDetectionConfig
    ) -> None:
        # z = (140 - 100)/10 = 4.0 > 3.0
        alert = detect_amount_anomaly(140.0, baseline, config)
        assert alert is not None
        assert alert.alert_type == "amount"
        assert alert.severity in list(AnomalySeverity)

    def test_insufficient_observations_returns_none(self, config: AnomalyDetectionConfig) -> None:
        baseline = AgentBaseline(
            agent_id="a1",
            zone_id="z1",
            mean_amount=100.0,
            std_amount=10.0,
            mean_frequency=5.0,
            counterparty_count=3,
            computed_at=NOW,
            observation_count=5,  # < min_observations
        )
        assert detect_amount_anomaly(200.0, baseline, config) is None


# ── detect_frequency_anomaly ────────────────────────────────────────────


class TestDetectFrequencyAnomaly:
    @pytest.fixture()
    def baseline(self) -> AgentBaseline:
        return AgentBaseline(
            agent_id="a1",
            zone_id="z1",
            mean_amount=100.0,
            std_amount=10.0,
            mean_frequency=5.0,
            counterparty_count=3,
            computed_at=NOW,
            observation_count=20,
        )

    @pytest.fixture()
    def config(self) -> AnomalyDetectionConfig:
        return AnomalyDetectionConfig(z_score_threshold=3.0, min_observations=10)

    def test_insufficient_observations(self, config: AnomalyDetectionConfig) -> None:
        baseline = AgentBaseline(
            agent_id="a1",
            zone_id="z1",
            mean_amount=100.0,
            std_amount=10.0,
            mean_frequency=5.0,
            counterparty_count=3,
            computed_at=NOW,
            observation_count=5,
        )
        assert detect_frequency_anomaly(100, baseline, config) is None

    def test_normal_frequency_returns_none(
        self, baseline: AgentBaseline, config: AnomalyDetectionConfig
    ) -> None:
        assert detect_frequency_anomaly(6, baseline, config) is None

    def test_high_frequency_returns_alert(
        self, baseline: AgentBaseline, config: AnomalyDetectionConfig
    ) -> None:
        # Very high count should trigger
        alert = detect_frequency_anomaly(100, baseline, config)
        assert alert is not None
        assert alert.alert_type == "frequency"


# ── detect_counterparty_anomaly ─────────────────────────────────────────


class TestDetectCounterpartyAnomaly:
    def test_known_counterparty_returns_none(self) -> None:
        known = {"cp1", "cp2", "cp3"}
        assert detect_counterparty_anomaly("cp1", known, "a1", "z1") is None

    def test_unknown_counterparty_returns_alert(self) -> None:
        known = {"cp1", "cp2"}
        alert = detect_counterparty_anomaly("cp_new", known, "a1", "z1")
        assert alert is not None
        assert alert.alert_type == "counterparty"
        assert alert.severity == AnomalySeverity.LOW


# ── _z_to_severity ──────────────────────────────────────────────────────


class TestZToSeverity:
    def test_at_1x_threshold_is_low(self) -> None:
        assert _z_to_severity(3.0, 3.0) == AnomalySeverity.LOW

    def test_at_1_5x_threshold_is_medium(self) -> None:
        assert _z_to_severity(4.5, 3.0) == AnomalySeverity.MEDIUM

    def test_at_2x_threshold_is_high(self) -> None:
        assert _z_to_severity(6.0, 3.0) == AnomalySeverity.HIGH

    def test_at_3x_threshold_is_critical(self) -> None:
        assert _z_to_severity(9.0, 3.0) == AnomalySeverity.CRITICAL

    def test_between_thresholds(self) -> None:
        # 3.1 is between 1x (3.0) and 1.5x (4.5) → LOW
        assert _z_to_severity(3.1, 3.0) == AnomalySeverity.LOW
        # 5.0 is between 1.5x (4.5) and 2x (6.0) → MEDIUM
        assert _z_to_severity(5.0, 3.0) == AnomalySeverity.MEDIUM
