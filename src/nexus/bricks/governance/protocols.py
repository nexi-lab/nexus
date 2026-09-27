"""Governance protocols — re-exports from contracts/protocols.

Canonical home is ``nexus.contracts.protocols.governance``.
This module re-exports for internal brick use.

Issue #1359: Protocol interfaces for governance services.
"""

from nexus.contracts.protocols.governance import (
    AnomalyDetectorProtocol as AnomalyDetectorProtocol,
)
from nexus.contracts.protocols.governance import (
    AnomalyServiceProtocol as AnomalyServiceProtocol,
)
from nexus.contracts.protocols.governance import (
    CollusionServiceProtocol as CollusionServiceProtocol,
)
from nexus.contracts.protocols.governance import (
    GovernanceGraphProtocol as GovernanceGraphProtocol,
)

__all__ = [
    "AnomalyDetectorProtocol",
    "AnomalyServiceProtocol",
    "CollusionServiceProtocol",
    "GovernanceGraphProtocol",
]
