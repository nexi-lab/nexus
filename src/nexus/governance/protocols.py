"""Governance protocols — dependency inversion for pluggable detection.

Issue #1359: AnomalyDetectorProtocol allows swapping statistical detection
for ML-based detection without changing service code.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from nexus.governance.models import AnomalyAlert, TransactionSummary


@runtime_checkable
class AnomalyDetectorProtocol(Protocol):
    """Protocol for anomaly detection implementations.

    Default: StatisticalAnomalyDetector (Z-score, IQR).
    Future: ML-based detector can swap in via this interface.
    """

    def detect(self, transaction: TransactionSummary) -> list[AnomalyAlert]:
        """Analyze a transaction and return any anomaly alerts.

        Args:
            transaction: The transaction to analyze.

        Returns:
            List of anomaly alerts (empty if no anomalies detected).
        """
        ...
