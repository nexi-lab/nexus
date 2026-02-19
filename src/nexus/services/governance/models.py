"""Backward-compatible re-exports (Issue #2129).

Canonical location: ``nexus.bricks.governance.models``
"""

from nexus.bricks.governance.models import AgentBaseline as AgentBaseline
from nexus.bricks.governance.models import AnomalyAlert as AnomalyAlert
from nexus.bricks.governance.models import AnomalyDetectionConfig as AnomalyDetectionConfig
from nexus.bricks.governance.models import AnomalySeverity as AnomalySeverity
from nexus.bricks.governance.models import ConstraintCheckResult as ConstraintCheckResult
from nexus.bricks.governance.models import ConstraintType as ConstraintType
from nexus.bricks.governance.models import EdgeType as EdgeType
from nexus.bricks.governance.models import FraudRing as FraudRing
from nexus.bricks.governance.models import FraudScore as FraudScore
from nexus.bricks.governance.models import GovernanceEdge as GovernanceEdge
from nexus.bricks.governance.models import GovernanceNode as GovernanceNode
from nexus.bricks.governance.models import NodeType as NodeType
from nexus.bricks.governance.models import RingType as RingType
from nexus.bricks.governance.models import SuspensionRecord as SuspensionRecord
from nexus.bricks.governance.models import ThrottleConfig as ThrottleConfig
from nexus.bricks.governance.models import TransactionSummary as TransactionSummary
