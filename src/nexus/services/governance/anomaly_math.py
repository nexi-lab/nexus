"""Backward-compatible re-exports (Issue #2129).

Canonical location: ``nexus.bricks.governance.anomaly_math``
"""

from nexus.bricks.governance.anomaly_math import compute_baseline as compute_baseline
from nexus.bricks.governance.anomaly_math import compute_iqr_bounds as compute_iqr_bounds
from nexus.bricks.governance.anomaly_math import compute_z_score as compute_z_score
from nexus.bricks.governance.anomaly_math import detect_amount_anomaly as detect_amount_anomaly
from nexus.bricks.governance.anomaly_math import (
    detect_counterparty_anomaly as detect_counterparty_anomaly,
)
from nexus.bricks.governance.anomaly_math import (
    detect_frequency_anomaly as detect_frequency_anomaly,
)
