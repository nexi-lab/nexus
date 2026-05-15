"""RPC result types extracted from protocol.py.

These are response types (not request params) used by ReBAC operations.
"""

from dataclasses import dataclass


@dataclass
class RebacCheckResult:
    """Result of rebac_check() with consistency metadata (Issue #1081).

    Following the SpiceDB/Zanzibar pattern, check results include
    consistency metadata for debugging and verification.
    """

    allowed: bool
    consistency_token: str
    cached: bool
    decision_time_ms: float


@dataclass
class RebacCreateResult:
    """Result of rebac_create() with consistency metadata (Issue #1081).

    Following the Zanzibar zookie pattern, writes return a consistency token
    that can be used for subsequent read-your-writes queries.

    Example:
        result = nx.rebac_create(subject, relation, object)
        # Use result.revision for audit trail
        allowed = nx.rebac_check(subject, permission, object)
    """

    tuple_id: str
    revision: int
    consistency_token: str
