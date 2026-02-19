"""Backward-compatible re-exports (Issue #2129).

Canonical location: ``nexus.bricks.governance.governance_wrapper``
"""

from nexus.bricks.governance.governance_wrapper import (
    GovernanceApprovalRequired as GovernanceApprovalRequired,
)
from nexus.bricks.governance.governance_wrapper import (
    GovernanceBlockedError as GovernanceBlockedError,
)
from nexus.bricks.governance.governance_wrapper import (
    GovernanceEnforcedPayment as GovernanceEnforcedPayment,
)
