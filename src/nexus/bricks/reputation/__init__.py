"""Reputation Brick — Agent Reputation & Dispute Resolution (Issue #1356, #2131).

Self-contained brick for agent reputation management: feedback submission,
score querying, leaderboards, and dispute resolution.

Public API:
    ReputationService     - Feedback and reputation score management
    DisputeService        - Dispute lifecycle management
    ReputationEvent       - Immutable reputation event record
    ReputationScore       - Materialized reputation score
    DisputeRecord         - Immutable dispute lifecycle record
    compute_composite_score - Weighted composite scoring
    compute_beta_score    - Beta distribution expected value

Errors:
    DuplicateFeedbackError  - Duplicate feedback for exchange+rater
    InvalidTransitionError  - Invalid dispute state transition
    DisputeNotFoundError    - Dispute ID not found
    DuplicateDisputeError   - Duplicate dispute for exchange
"""

from nexus.bricks.reputation.dispute_service import DisputeService as DisputeService
from nexus.bricks.reputation.errors import DisputeNotFoundError as DisputeNotFoundError
from nexus.bricks.reputation.errors import DuplicateDisputeError as DuplicateDisputeError
from nexus.bricks.reputation.errors import DuplicateFeedbackError as DuplicateFeedbackError
from nexus.bricks.reputation.errors import InvalidTransitionError as InvalidTransitionError
from nexus.bricks.reputation.reputation_records import DisputeRecord as DisputeRecord
from nexus.bricks.reputation.reputation_records import ReputationEvent as ReputationEvent
from nexus.bricks.reputation.reputation_records import ReputationScore as ReputationScore
from nexus.bricks.reputation.reputation_service import ReputationService as ReputationService
