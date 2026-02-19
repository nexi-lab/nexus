"""Agent Delegation API — backward-compatibility re-exports (Issue #2131).

The delegation brick has been moved to ``nexus.bricks.delegation``.
This module re-exports the public API for backward compatibility.
"""

from nexus.bricks.delegation.derivation import GrantSpec as GrantSpec
from nexus.bricks.delegation.derivation import derive_grants as derive_grants
from nexus.bricks.delegation.errors import DelegationChainError as DelegationChainError
from nexus.bricks.delegation.errors import DelegationError as DelegationError
from nexus.bricks.delegation.errors import DelegationNotFoundError as DelegationNotFoundError
from nexus.bricks.delegation.errors import DepthExceededError as DepthExceededError
from nexus.bricks.delegation.errors import EscalationError as EscalationError
from nexus.bricks.delegation.errors import InsufficientTrustError as InsufficientTrustError
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
