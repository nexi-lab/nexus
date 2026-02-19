"""Reputation domain errors (Issue #2131).

Hierarchy:
    DuplicateFeedbackError    - Duplicate feedback for same exchange+rater
    InvalidTransitionError    - Invalid dispute state transition
    DisputeNotFoundError      - Dispute ID not found
    DuplicateDisputeError     - Dispute already exists for exchange
"""

from __future__ import annotations


class DuplicateFeedbackError(Exception):
    """Raised when duplicate feedback is submitted for the same exchange+rater."""


class InvalidTransitionError(Exception):
    """Raised when a dispute state transition is not valid."""


class DisputeNotFoundError(Exception):
    """Raised when a dispute is not found."""


class DuplicateDisputeError(Exception):
    """Raised when a dispute already exists for an exchange."""
