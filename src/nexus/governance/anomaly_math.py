"""Pure math functions for anomaly detection.

Issue #1359 Phase 1: Z-score, IQR, baseline computation.
No ORM or service dependencies — pure functions only.

Pattern follows: nexus.services.reputation.reputation_math
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from nexus.governance.models import (
    AgentBaseline,
    AnomalyAlert,
    AnomalyDetectionConfig,
    AnomalySeverity,
    TransactionSummary,
)


def compute_z_score(value: float, mean: float, std: float) -> float:
    """Compute the Z-score of a value relative to a distribution.

    Returns 0.0 if std is zero or negative (no variation = no anomaly).

    Args:
        value: The observed value.
        mean: Distribution mean.
        std: Distribution standard deviation.

    Returns:
        Z-score (number of standard deviations from mean).
    """
    if std <= 0.0:
        return 0.0
    return (value - mean) / std


def compute_iqr_bounds(values: Sequence[float]) -> tuple[float, float]:
    """Compute IQR-based anomaly bounds.

    Args:
        values: Sequence of observed values (must have at least 4 elements).

    Returns:
        Tuple of (lower_bound, upper_bound) where values outside
        are considered anomalous.

    Raises:
        ValueError: If fewer than 4 values provided.
    """
    if len(values) < 4:
        msg = f"Need at least 4 values for IQR computation, got {len(values)}"
        raise ValueError(msg)

    sorted_vals = sorted(values)
    n = len(sorted_vals)

    q1_idx = n / 4.0
    q3_idx = 3 * n / 4.0

    q1 = _interpolate(sorted_vals, q1_idx)
    q3 = _interpolate(sorted_vals, q3_idx)

    iqr = q3 - q1
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr

    return lower, upper


def _interpolate(sorted_vals: list[float], idx: float) -> float:
    """Linear interpolation for percentile calculation."""
    lower_idx = int(math.floor(idx))
    upper_idx = int(math.ceil(idx))

    if lower_idx == upper_idx or upper_idx >= len(sorted_vals):
        return sorted_vals[min(lower_idx, len(sorted_vals) - 1)]

    fraction = idx - lower_idx
    return sorted_vals[lower_idx] + fraction * (sorted_vals[upper_idx] - sorted_vals[lower_idx])


def compute_baseline(
    transactions: Sequence[TransactionSummary],
    agent_id: str,
    zone_id: str,
) -> AgentBaseline:
    """Compute statistical baseline from historical transactions.

    Args:
        transactions: Historical transactions for the agent.
        agent_id: Agent identifier.
        zone_id: Zone identifier.

    Returns:
        AgentBaseline with computed statistics.
    """
    if not transactions:
        return AgentBaseline(
            agent_id=agent_id,
            zone_id=zone_id,
            mean_amount=0.0,
            std_amount=0.0,
            mean_frequency=0.0,
            counterparty_count=0,
            computed_at=datetime.now(UTC),
            observation_count=0,
        )

    amounts = [t.amount for t in transactions]
    counterparties = {t.counterparty for t in transactions}

    mean_amount = sum(amounts) / len(amounts)
    variance = sum((a - mean_amount) ** 2 for a in amounts) / len(amounts)
    std_amount = math.sqrt(variance)

    # Frequency: transactions per day based on time span
    timestamps = sorted(t.timestamp for t in transactions)
    if len(timestamps) > 1:
        span_seconds = (timestamps[-1] - timestamps[0]).total_seconds()
        span_days = max(span_seconds / 86400.0, 1.0)
        mean_frequency = len(transactions) / span_days
    else:
        mean_frequency = 1.0

    return AgentBaseline(
        agent_id=agent_id,
        zone_id=zone_id,
        mean_amount=mean_amount,
        std_amount=std_amount,
        mean_frequency=mean_frequency,
        counterparty_count=len(counterparties),
        computed_at=datetime.now(UTC),
        observation_count=len(transactions),
    )


def detect_amount_anomaly(
    amount: float,
    baseline: AgentBaseline,
    config: AnomalyDetectionConfig,
) -> AnomalyAlert | None:
    """Detect if a transaction amount is anomalous.

    Uses Z-score comparison against the agent's baseline.

    Returns:
        AnomalyAlert if anomalous, None if normal.
    """
    if baseline.observation_count < config.min_observations:
        return None

    z = compute_z_score(amount, baseline.mean_amount, baseline.std_amount)

    if abs(z) < config.z_score_threshold:
        return None

    severity = _z_to_severity(abs(z), config.z_score_threshold)

    return AnomalyAlert(
        alert_id=str(uuid.uuid4()),
        agent_id=baseline.agent_id,
        zone_id=baseline.zone_id,
        severity=severity,
        alert_type="amount",
        details={
            "amount": amount,
            "z_score": round(z, 4),
            "baseline_mean": baseline.mean_amount,
            "baseline_std": baseline.std_amount,
            "threshold": config.z_score_threshold,
        },
        created_at=datetime.now(UTC),
    )


def detect_frequency_anomaly(
    recent_count: int,
    baseline: AgentBaseline,
    config: AnomalyDetectionConfig,
) -> AnomalyAlert | None:
    """Detect if transaction frequency is anomalous.

    Compares recent transaction count against baseline frequency.

    Returns:
        AnomalyAlert if anomalous, None if normal.
    """
    if baseline.observation_count < config.min_observations:
        return None

    if baseline.mean_frequency <= 0:
        return None

    # Z-score of recent count vs expected (mean_frequency is per day)
    # Use sqrt of mean as crude std estimate for count data (Poisson-like)
    std_estimate = math.sqrt(max(baseline.mean_frequency, 1.0))
    z = compute_z_score(float(recent_count), baseline.mean_frequency, std_estimate)

    if abs(z) < config.z_score_threshold:
        return None

    severity = _z_to_severity(abs(z), config.z_score_threshold)

    return AnomalyAlert(
        alert_id=str(uuid.uuid4()),
        agent_id=baseline.agent_id,
        zone_id=baseline.zone_id,
        severity=severity,
        alert_type="frequency",
        details={
            "recent_count": recent_count,
            "z_score": round(z, 4),
            "baseline_frequency": baseline.mean_frequency,
            "threshold": config.z_score_threshold,
        },
        created_at=datetime.now(UTC),
    )


def detect_counterparty_anomaly(
    counterparty: str,
    known_counterparties: set[str],
    agent_id: str,
    zone_id: str,
) -> AnomalyAlert | None:
    """Detect if a transaction involves an unknown counterparty.

    Returns:
        AnomalyAlert if counterparty is new, None if known.
    """
    if counterparty in known_counterparties:
        return None

    return AnomalyAlert(
        alert_id=str(uuid.uuid4()),
        agent_id=agent_id,
        zone_id=zone_id,
        severity=AnomalySeverity.LOW,
        alert_type="counterparty",
        details={
            "new_counterparty": counterparty,
            "known_count": len(known_counterparties),
        },
        created_at=datetime.now(UTC),
    )


def _z_to_severity(abs_z: float, threshold: float) -> AnomalySeverity:
    """Map absolute Z-score to severity level."""
    if abs_z >= threshold * 3:
        return AnomalySeverity.CRITICAL
    if abs_z >= threshold * 2:
        return AnomalySeverity.HIGH
    if abs_z >= threshold * 1.5:
        return AnomalySeverity.MEDIUM
    return AnomalySeverity.LOW
