"""Delegation domain errors (Issue #1271).

Hierarchy:
    DelegationError (base)
    ├── EscalationError        - Anti-escalation invariant violation
    ├── TooManyGrantsError     - Exceeds MAX_DELEGATABLE_GRANTS
    ├── InvalidDelegationModeError - Unknown mode value
    ├── DelegationNotFoundError   - Delegation ID not found
    ├── DelegationChainError      - Delegated agent tries to delegate
"""


class DelegationError(Exception):
    """Base error for delegation operations."""


class EscalationError(DelegationError):
    """Raised when delegation would escalate privileges beyond parent grants.

    This is the core anti-escalation invariant: child_grants subset-of parent_grants.
    """


class TooManyGrantsError(DelegationError):
    """Raised when derived grants exceed MAX_DELEGATABLE_GRANTS (1000)."""


class InvalidDelegationModeError(DelegationError):
    """Raised when an unknown delegation mode is specified."""


class DelegationNotFoundError(DelegationError):
    """Raised when a delegation_id does not exist."""


class DelegationChainError(DelegationError):
    """Raised when a delegated agent attempts to delegate.

    v1 constraint: no delegation chains (A -> B -> C is forbidden).
    With #1618: chains allowed when can_sub_delegate=True and depth < max_depth.
    """


class DepthExceededError(DelegationError):
    """Raised when sub-delegation would exceed the max_depth limit."""


class InvalidPrefixError(DelegationError):
    """Raised when scope_prefix fails validation (empty, relative, malformed)."""
