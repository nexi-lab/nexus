"""Agent Delegation API — NS Derivation + Delegated Identity (Issue #1271).

Self-contained brick for coordinator agents to provision worker agent
identities with narrower permissions. Follows the ``pay/`` exemplary
pattern: domain models, pure derivation logic, service, and errors.

Public API:
    DelegationService     — Orchestrates delegation lifecycle
    DelegationRecord      — Immutable snapshot of a delegation
    DelegationResult      — Return type from delegate()
    DelegationMode        — Enum: COPY, CLEAN, SHARED
    derive_grants         — Pure function: parent grants → child grants
    GrantSpec             — Single grant to materialize

Errors:
    DelegationError       — Base
    EscalationError       — Anti-escalation violation
    TooManyGrantsError    — > MAX_DELEGATABLE_GRANTS
    InvalidDelegationModeError — Unknown mode
    DelegationNotFoundError — ID not found
    DelegationChainError  — Delegated agent tries to delegate
"""

from nexus.services.delegation.derivation import GrantSpec as GrantSpec
from nexus.services.delegation.derivation import derive_grants as derive_grants
from nexus.services.delegation.errors import DelegationChainError as DelegationChainError
from nexus.services.delegation.errors import DelegationError as DelegationError
from nexus.services.delegation.errors import DelegationNotFoundError as DelegationNotFoundError
from nexus.services.delegation.errors import EscalationError as EscalationError
from nexus.services.delegation.errors import (
    InvalidDelegationModeError as InvalidDelegationModeError,
)
from nexus.services.delegation.errors import TooManyGrantsError as TooManyGrantsError
from nexus.services.delegation.models import DelegationMode as DelegationMode
from nexus.services.delegation.models import DelegationRecord as DelegationRecord
from nexus.services.delegation.models import DelegationResult as DelegationResult
from nexus.services.delegation.service import DelegationService as DelegationService
