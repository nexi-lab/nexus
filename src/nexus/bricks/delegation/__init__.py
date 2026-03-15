"""Delegation Brick — Agent Identity Delegation (Issue #1271, #1618, #2131).

Self-contained brick for coordinator agents to provision worker agent
identities with narrower permissions. Follows the NEXUS-LEGO-ARCHITECTURE
brick pattern: domain models, pure derivation logic, service, and errors.

Public API:
    DelegationService     - Orchestrates delegation lifecycle
    DelegationRecord      - Immutable snapshot of a delegation
    DelegationResult      - Return type from delegate()
    DelegationMode        - Enum: COPY, CLEAN, SHARED
    DelegationStatus      - Enum: ACTIVE, REVOKED, EXPIRED, COMPLETED
    DelegationScope       - Fine-grained scope constraints
    DelegationOutcome     - Enum: COMPLETED, FAILED, TIMEOUT (#1619)
    derive_grants         - Pure function: parent grants -> child grants
    GrantSpec             - Single grant to materialize

Errors:
    DelegationError       - Base
    EscalationError       - Anti-escalation violation
    TooManyGrantsError    - > MAX_DELEGATABLE_GRANTS
    InvalidDelegationModeError - Unknown mode
    DelegationNotFoundError - ID not found
    DelegationChainError  - Delegated agent tries to delegate
    DepthExceededError    - Sub-delegation depth exceeded
    InvalidPrefixError    - Malformed scope_prefix
"""

from nexus.bricks.delegation.derivation import GrantSpec as GrantSpec
from nexus.bricks.delegation.derivation import derive_grants as derive_grants
from nexus.bricks.delegation.errors import DelegationChainError as DelegationChainError
from nexus.bricks.delegation.errors import DelegationError as DelegationError
from nexus.bricks.delegation.errors import DelegationNotFoundError as DelegationNotFoundError
from nexus.bricks.delegation.errors import DepthExceededError as DepthExceededError
from nexus.bricks.delegation.errors import EscalationError as EscalationError
from nexus.bricks.delegation.errors import (
    InvalidDelegationModeError as InvalidDelegationModeError,
)
from nexus.bricks.delegation.errors import InvalidPrefixError as InvalidPrefixError
from nexus.bricks.delegation.errors import TooManyGrantsError as TooManyGrantsError
from nexus.bricks.delegation.models import DelegationMode as DelegationMode
from nexus.bricks.delegation.models import DelegationOutcome as DelegationOutcome
from nexus.bricks.delegation.models import DelegationRecord as DelegationRecord
from nexus.bricks.delegation.models import DelegationResult as DelegationResult
from nexus.bricks.delegation.models import DelegationScope as DelegationScope
from nexus.bricks.delegation.models import DelegationStatus as DelegationStatus
from nexus.bricks.delegation.service import DelegationService as DelegationService
